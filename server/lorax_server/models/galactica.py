import re
from typing import List, Optional, Type

import torch
import torch.distributed
from loguru import logger
from transformers import (
    AutoConfig,
    AutoTokenizer,
    PreTrainedTokenizerBase,
)

from lorax_server.models.causal_lm import CausalLM, CausalLMBatch
from lorax_server.models.custom_modeling.opt_modeling import OPTForCausalLM
from lorax_server.pb import generate_pb2
from lorax_server.utils import (
    NextTokenChooser,
    StoppingCriteria,
    Weights,
    initialize_torch_distributed,
    weight_files,
)
from lorax_server.utils.tokenizer import TokenizerManager

# CREDIT: Papers with code => https://github.com/paperswithcode/galai/blob/main/galai/utils.py

# we split individual characters inside special tokens like [START_DNA]
CUSTOM_SEQ_RE = re.compile(r"(\[START_(DNA|SMILES|I_SMILES|AMINO)])(.*?)(\[END_\2])")

# token added to implement a custom sequence tokenization. This token is added at
# corpus cleaning step and removed in pretokenization. The digits are added to increase the chance
# that they do not occur in the corpus. The digits are escaped so that the token does not appear
# literally in the source code in case we ever include it in the training data.
SPLIT_MARKER = f"SPL{1}T-TH{1}S-Pl3A5E"


def _insert_split_marker(m: re.Match):
    """
    Applies split marker based on a regex match of special tokens such as
    [START_DNA].
    Parameters
    ----------
    n : str
        Input text to split
    Returns
    ----------
    str - the text with the split token added
    """
    start_token, _, sequence, end_token = m.groups()
    sequence = re.sub(r"(.)", rf"{SPLIT_MARKER}\1", sequence, flags=re.DOTALL)
    return f"{start_token}{sequence}{SPLIT_MARKER}{end_token}"


def escape_custom_split_sequence(text):
    """
    Applies custom splitting to the text for GALILEO's tokenization
    Parameters
    ----------
    text : str
        Input text to split
    Returns
    ----------
    str - the text with the split token added
    """
    return CUSTOM_SEQ_RE.sub(_insert_split_marker, text)


# END CREDIT


class GalacticaCausalLMBatch(CausalLMBatch):
    @classmethod
    def from_pb(
        cls,
        pb: generate_pb2.Batch,
        tokenizer: PreTrainedTokenizerBase,
        tokenizers: TokenizerManager,
        processor,
        config,
        dtype: torch.dtype,
        device: torch.device,
    ) -> "GalacticaCausalLMBatch":
        inputs = []
        next_token_choosers = []
        stopping_criterias = []
        prefix_offsets = []
        read_offsets = []
        requests_idx_mapping = {}

        # Parse batch
        max_truncation = 0
        padding_right_offset = 0
        max_decode_tokens = 0
        for i, r in enumerate(pb.requests):
            requests_idx_mapping[r.id] = i
            # Add escape_custom_split_sequence to the CausalLMBatch logic
            req_inputs = tokenizers.get_inputs(r, tokenizer)
            inputs.append(escape_custom_split_sequence(req_inputs))
            next_token_choosers.append(NextTokenChooser.from_pb(r.parameters, device, tokenizer))
            stopping_criteria = StoppingCriteria.from_pb(r.stopping_parameters, tokenizer)
            stopping_criterias.append(stopping_criteria)
            max_truncation = max(max_truncation, r.truncate)
            max_decode_tokens += stopping_criteria.max_new_tokens
            padding_right_offset = max(padding_right_offset, stopping_criteria.max_new_tokens)

        tokenized_inputs = tokenizer(
            inputs,
            return_tensors="pt",
            padding=True,
            return_token_type_ids=False,
            truncation=True,
            max_length=max_truncation,
        ).to(device)
        for _ in pb.requests:
            input_len = tokenized_inputs["input_ids"].shape[1]
            prefix_offsets.append(0)
            read_offsets.append(input_len)

        input_lengths = tokenized_inputs["attention_mask"].sum(1)
        max_input_length = input_lengths.max()

        input_ids = tokenized_inputs["input_ids"]
        # Allocate maximum attention_mask
        attention_mask = input_ids.new_zeros((pb.size, max_input_length + padding_right_offset))
        # Copy tokenizer attention_mask into fully allocated attention_mask
        attention_mask[:, :max_input_length] = tokenized_inputs["attention_mask"]

        position_ids = tokenized_inputs["attention_mask"].long().cumsum(-1) - 1
        position_ids.masked_fill_(tokenized_inputs["attention_mask"] == 0, 1)
        all_input_ids = tokenized_inputs["input_ids"].T.split(1, dim=1)

        max_tokens = len(inputs) * max_input_length + max_decode_tokens

        return cls(
            batch_id=pb.id,
            requests=pb.requests,
            requests_idx_mapping=requests_idx_mapping,
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=None,
            all_input_ids=list(all_input_ids),
            input_lengths=input_lengths.tolist(),
            prefix_offsets=prefix_offsets,
            read_offsets=read_offsets,
            next_token_choosers=next_token_choosers,
            stopping_criterias=stopping_criterias,
            max_input_length=max_input_length.item(),
            padding_right_offset=padding_right_offset,
            max_tokens=max_tokens,
        )


class GalacticaSharded(CausalLM):
    def __init__(
        self,
        model_id: str,
        revision: Optional[str] = None,
        quantize: Optional[str] = None,
        compile: bool = False,
        dtype: Optional[torch.dtype] = None,
        trust_remote_code: bool = False,
    ):
        if compile:
            logger.info(f"Model {model_id} does not support CUDA graph compilation. Skipping compilation.")

        self.process_group, rank, world_size = initialize_torch_distributed()
        if torch.cuda.is_available():
            device = torch.device(f"cuda:{rank}")
            dtype = torch.float16 if dtype is None else dtype
        else:
            device = torch.device("cpu")
            dtype = torch.float32

        tokenizer = AutoTokenizer.from_pretrained(
            model_id,
            revision=revision,
            padding_side="left",
            truncation_side="left",
            trust_remote_code=trust_remote_code,
        )

        config = AutoConfig.from_pretrained(
            model_id,
            revision=revision,
            tp_parallel=True,
            trust_remote_code=trust_remote_code,
        )
        config.quantize = quantize
        tokenizer.pad_token_id = config.pad_token_id

        torch.distributed.barrier(group=self.process_group)
        filenames = weight_files(model_id, revision=revision, extension=".safetensors")
        weights = Weights(filenames, device=device, dtype=dtype, process_group=self.process_group)
        weights._set_config(model_id, config)

        model = OPTForCausalLM(config, weights)

        torch.distributed.barrier(group=self.process_group)
        super(CausalLM, self).__init__(
            model_id=model_id,
            model=model,
            tokenizer=tokenizer,
            requires_padding=True,
            dtype=dtype,
            device=device,
            rank=rank,
            world_size=world_size,
            trust_remote_code=trust_remote_code,
        )

    @property
    def batch_type(self) -> Type[CausalLMBatch]:
        return GalacticaCausalLMBatch

    def decode(self, generated_ids: List[int]) -> str:
        # Do not skip special tokens as they are used for custom parsing rules of the generated text
        return self.tokenizer.decode(generated_ids, skip_special_tokens=False, clean_up_tokenization_spaces=False)

    def forward(self, input_ids, attention_mask, position_ids, past_key_values: Optional = None):
        outputs = self.model.forward(
            input_ids=input_ids,
            attention_mask=attention_mask,
            past_key_values=past_key_values,
            use_cache=True,
        )
        return outputs.logits, outputs.past_key_values
