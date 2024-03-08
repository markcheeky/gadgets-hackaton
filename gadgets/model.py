from __future__ import annotations

import contextlib
import unittest.mock
import warnings
from typing import Any, Callable, List, Optional

import bs4
import torch
import numpy as np
import pandas as pd
import transformers
from transformers import GenerationConfig, LogitsProcessorList, StoppingCriteriaList

from gadgets.gadget import Calculator, Gadget
from gadgets.markup import GADGET_TAG, OUTPUT_TAG, RESULT_TAG


class StopAfterGadgetCall(transformers.generation.StoppingCriteria):
    def __init__(self, tokenizer: transformers.PreTrainedTokenizer) -> None:
        self.tokenizer = tokenizer
        self.closing_tag_ids: torch.Tensor = self.tokenizer(
            "</" + GADGET_TAG + ">", add_special_tokens=False, return_tensors="pt"
        ).input_ids.flatten()
        self.mask = torch.tensor([])

    def __call__(self, seq_ids: torch.Tensor, scores: torch.Tensor, **kwargs) -> bool:
        if seq_ids.shape[-1] < self.closing_tag_ids.shape[-1]:
            return False

        # check if </gadget> is at the end of the sequence
        self.closing_tag_ids = self.closing_tag_ids.to(seq_ids.device)
        ending = seq_ids[..., -self.closing_tag_ids.shape[-1] :]
        self.mask = (ending == self.closing_tag_ids).all(dim=-1)
        return self.mask.any()


@contextlib.contextmanager
def set_padding_side(tokenizer: transformers.PreTrainedTokenizer, side: str):
    original_padding_side = tokenizer.padding_side
    tokenizer.padding_side = side
    try:
        yield
    finally:
        tokenizer.padding_side = original_padding_side


class GadgetAssist(transformers.GenerationMixin):
    """
    Mixin that overrides model.generate to support the
    model with external gadgets.
    """

    def prepare_for_generate(
        self,
        tokenizer: transformers.PreTrainedTokenizer,
        enabled_gadgets: list[Gadget],
        default_max_tokens: int = 1000,
    ) -> None:
        self.tokenizer = tokenizer
        self.enabled_gadgets = enabled_gadgets
        self.default_max_tokens = default_max_tokens

    @torch.no_grad()
    def generate(
        self,
        input_ids: Optional[torch.Tensor] = None,
        generation_config: Optional[GenerationConfig] = None,
        logits_processor: Optional[LogitsProcessorList] = None,
        stopping_criteria: Optional[StoppingCriteriaList] = None,
        prefix_allowed_tokens_fn: Optional[Callable[[int, torch.Tensor], List[int]]] = None,
        synced_gpus: Optional[bool] = None,
        streamer: Optional[transformers.generation.streamers.BaseStreamer] = None,
        #cache_encoder_output: bool = False,
        **kwargs,
        # signature of GenerationMixin.generate() in Transformers==4.28.1, with inputs<=>input_ids
    ) -> torch.LongTensor:
        """
        Model is expected to generate gadget tags.
        Whenever a gadget tag is generated, the gadget is called,
        and the output is fed back into the model inside of an output tag.

        Final result is expected to be in result tag.
        """
        assert isinstance(self, transformers.PreTrainedModel)

        stop_after_gadget_call = StopAfterGadgetCall(self.tokenizer)
        stopping_criteria = transformers.generation.StoppingCriteriaList([stop_after_gadget_call])

        if kwargs is None:
            kwargs = {}

        running_gadgets: dict[str, Gadget] = {g.gadget_id(): g for g in self.enabled_gadgets}

        max_tokens = None
        min_tokens = None

        if "max_length" in kwargs:
            max_length = kwargs.pop("max_length")
            if max_length is not None:
                max_tokens = max_length - input_ids.shape[-1]
        if "min_length" in kwargs:
            min_length = kwargs.pop("min_length")
            if min_length is not None:
                min_tokens = min_length - input_ids.shape[-1]
        if "max_new_tokens" in kwargs:
            max_tokens = kwargs.pop("max_new_tokens")
        if "min_new_tokens" in kwargs:
            min_tokens = kwargs.pop("min_new_tokens")

        if max_tokens is None:
            max_tokens = self.default_max_tokens

        outputs_str = pd.Series([""] * input_ids.shape[0])

        # if self.config.is_encoder_decoder and cache_encoder_output:
        #     encoder_outputs = self.get_encoder()(input_ids)
        # else:
        #     encoder_outputs = None

        runnings = np.array([True] * input_ids.shape[0], dtype=bool)
        decoder_start_token = self.tokenizer.convert_ids_to_tokens(self.config.decoder_start_token_id)
        attention_mask = kwargs.pop("attention_mask", None)

        while True:
            if not runnings.any():
                break

            with set_padding_side(self.tokenizer, "left"):
                encoded = self.tokenizer(
                    text_target=(decoder_start_token + outputs_str[runnings]).tolist(),
                    add_special_tokens=False,
                    return_tensors="pt",
                    padding="longest",
                    return_attention_mask=True,
                ).to(self.device)
            decoder_input_ids = encoded.input_ids.to(self.device)
            decoder_attention_mask = encoded.attention_mask.to(self.device)

            num_total_tokens = decoder_input_ids.shape[-1]

            if num_total_tokens + 2 >= max_tokens:
                break

            if max_tokens is not None:
                kwargs["max_new_tokens"] = max_tokens - num_total_tokens
            if min_tokens is not None:
                kwargs["min_new_tokens"] = max(0, min_tokens - num_total_tokens)

            # if encoder_outputs is not None:
            #     encoder_outputs_curr = encoder_outputs.copy()
            #     for key, tensor in encoder_outputs_curr.items():
            #         encoder_outputs_curr[key] = tensor[runnings]
            #     encoder_kwargs = {"encoder_outputs": encoder_outputs_curr}
            # else:
            #     encoder_kwargs = {"input_ids": input_ids[runnings]}
            
            model_output = super().generate(
                input_ids=input_ids[runnings],
            #    **encoder_kwargs,
                attention_mask=attention_mask[runnings] if attention_mask is not None else None,
                decoder_input_ids=decoder_input_ids,
                decoder_attention_mask=decoder_attention_mask,
                stopping_criteria=stopping_criteria,
                generation_config=generation_config,
                **kwargs,
            )

            outputs_str[runnings] = self.tokenizer.batch_decode(
                model_output,
                skip_special_tokens=True,
                spaces_between_special_tokens=False,
            )

            curr_seq_idxs = np.arange(input_ids.shape[0])[runnings]

            for curr_seq_idx, glob_seq_idx in enumerate(curr_seq_idxs):
                if stop_after_gadget_call.mask[curr_seq_idx]:
                    try:
                        doc = bs4.BeautifulSoup(outputs_str[glob_seq_idx], features="html.parser")
                    except Exception as e:
                        warnings.warn(f"Failed to parse model output: {e}")
                        continue

                    gadget_tags: list[bs4.Tag] = doc.find_all(GADGET_TAG)
                    evaluated_something = False
                    for gadget_tag_input in gadget_tags:
                        next_el = gadget_tag_input.next_sibling
                        while (
                            next_el is not None and isinstance(next_el, bs4.NavigableString) and next_el.strip() == ""
                        ):
                            # skip whitespace
                            next_el = next_el.next_sibling
                        if isinstance(next_el, bs4.Tag) and next_el.name == OUTPUT_TAG:
                            # already evaluated this gadget tag
                            continue
                        evaluated_something = True
                        gadget_input = gadget_tag_input.get_text()
                        gadget_id = gadget_tag_input.get("id", None)
                        gadget = running_gadgets.get(gadget_id, None)
                        if gadget is None:
                            gadget_output = f"ERROR: Gadget '{gadget_id}' not found"
                        else:
                            gadget_output = gadget(gadget_input)

                        gadget_tag_output = doc.new_tag(OUTPUT_TAG)
                        gadget_tag_output.string = gadget_output
                        gadget_tag_input.insert_after(gadget_tag_output)
                        gadget_tag_input.insert_after("\n")
                        gadget_tag_output.insert_after("\n")

                    if evaluated_something:
                        # replace outputs_str with the evaluated version
                        outputs_str[glob_seq_idx] = str(doc)
                        
                elif self.tokenizer.eos_token_id in model_output[curr_seq_idx]:
                    runnings[glob_seq_idx] = False

        with set_padding_side(self.tokenizer, "left"):
            outputs_tensor = self.tokenizer(
                text=[""] * input_ids.shape[0],
                text_target=(decoder_start_token + outputs_str).tolist(),
                return_tensors="pt",
                padding="longest",
                add_special_tokens=True,
            ).labels.to(self.device)

        return outputs_tensor


def gadget_assisted_model(model_class: type[transformers.PreTrainedModel]):
    class GadgetAssistedModel(GadgetAssist, model_class):
        pass

    return GadgetAssistedModel


str_prompt = "Write xml tag gadget id attribute id='calculator' and fill '2 + 2' inside. "
str_let_me_think = "Let me think about it"
str_gadget_usage = f"<{GADGET_TAG} id='calculator'>2+2</{GADGET_TAG}>"
str_gadget_output = f"<{OUTPUT_TAG}>4</{OUTPUT_TAG}>"
str_result = "129818"
str_result_with_tag = f"Final answer is <{RESULT_TAG}>{str_result}</{RESULT_TAG}>."


def test_generate_check_outputs(
    model: transformers.PreTrainedModel,
    tokenizer: transformers.PreTrainedTokenizer,
    mocked_model_outputs: list[str],
    expected_full_outputs: list[str],
    expected_result: str | None,
    enabled_gadgets: list[Gadget],
) -> bool:
    assert isinstance(model, GadgetAssist)

    model.prepare_for_generate(
        tokenizer,
        enabled_gadgets=enabled_gadgets,
    )

    mocked_model_outputs_tokenized = [
        tokenizer(string, return_tensors="pt").input_ids for string in mocked_model_outputs
    ]

    with unittest.mock.patch("transformers.GenerationMixin.generate") as patched_model:
        patched_model.side_effect = mocked_model_outputs_tokenized
        full_output, result = model.generate(
            str_prompt,
            return_result=True,
            return_as_str=True,
            max_length=400,
            num_beams=3,
            num_return_sequences=1,
            no_repeat_ngram_size=1,
            remove_invalid_values=True,
        )

    expected_full_output = bs4.BeautifulSoup(
        " ".join(expected_full_outputs), features="html.parser"
    ).prettify()
    full_output = bs4.BeautifulSoup(full_output, features="html.parser").prettify()

    output_matches = _compare_strings_ignore_whitespace(full_output, expected_full_output)

    if expected_result is None:
        result_matches = result is None
    else:
        result_matches = _compare_strings_ignore_whitespace(result, expected_result)

    is_correct = output_matches and result_matches
    return is_correct


def _compare_strings_ignore_whitespace(str1: str, str2: str) -> bool:
    return " ".join(str1.split()) == " ".join(str2.split())


TESTS = [
    {
        "mocked": [str_result_with_tag],
        "expected_outputs": [str_result_with_tag],
        "expected_result": str_result,
    },
    {
        "mocked": [str_let_me_think, str_result_with_tag],
        "expected_outputs": [str_let_me_think, str_result_with_tag],
        "expected_result": str_result,
    },
    {
        "mocked": [str_gadget_usage, str_result_with_tag],
        "expected_outputs": [str_gadget_usage, str_gadget_output, str_result_with_tag],
        "expected_result": str_result,
    },
    {
        "mocked": [str_gadget_usage, str_gadget_usage, str_result_with_tag],
        "expected_outputs": [
            str_gadget_usage,
            str_gadget_output,
            str_gadget_usage,
            str_gadget_output,
            str_result_with_tag,
        ],
        "expected_result": str_result,
    },
    {
        "mocked": [str_gadget_usage + str_gadget_usage, str_result_with_tag],
        "expected_outputs": [
            str_gadget_usage + str_gadget_output + str_gadget_usage + str_gadget_output,
            str_result_with_tag,
        ],
        "expected_result": str_result,
    },
]


def test_generate_with_gadgets():
    model_name = "salesforce/codet5-small"
    tokenizer = transformers.RobertaTokenizer.from_pretrained(model_name)
    model = gadget_assisted_model(transformers.T5ForConditionalGeneration).from_pretrained(model_name)

    for i, test in enumerate(TESTS):
        assert test_generate_check_outputs(
            model,
            tokenizer,
            test["mocked"],
            test["expected_outputs"],
            test["expected_result"],
            enabled_gadgets=[Calculator()],
        )


if __name__ == "__main__":
    test_generate_with_gadgets()
