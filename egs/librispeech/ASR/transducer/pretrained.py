#!/usr/bin/env python3
# Copyright      2021  Xiaomi Corp.        (authors: Fangjun Kuang)
#
# See ../../../../LICENSE for clarification regarding multiple authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Usage:

./transducer/pretrained.py \
        --checkpoint ./transducer/exp/pretrained.pt \
        --bpe-model ./data/lang_bpe_500/bpe.model \
        --method greedy_search \
        /path/to/foo.wav \
        /path/to/bar.wav \

You can also use `./transducer/exp/epoch-xx.pt`.

Note: ./transducer/exp/pretrained.pt is generated by
./transducer/export.py
"""


import argparse
import logging
import math
from typing import List

import kaldifeat
import sentencepiece as spm
import torch
import torchaudio
from beam_search import beam_search, greedy_search
from conformer import Conformer
from decoder import Decoder
from joiner import Joiner
from model import Transducer
from torch.nn.utils.rnn import pad_sequence

from icefall.env import get_env_info
from icefall.utils import AttributeDict


def get_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument(
        "--checkpoint",
        type=str,
        required=True,
        help=(
            "Path to the checkpoint. "
            "The checkpoint is assumed to be saved by "
            "icefall.checkpoint.save_checkpoint()."
        ),
    )

    parser.add_argument(
        "--bpe-model",
        type=str,
        help="""Path to bpe.model.
        Used only when method is ctc-decoding.
        """,
    )

    parser.add_argument(
        "--method",
        type=str,
        default="greedy_search",
        help="""Possible values are:
          - greedy_search
          - beam_search
        """,
    )

    parser.add_argument(
        "sound_files",
        type=str,
        nargs="+",
        help=(
            "The input sound file(s) to transcribe. "
            "Supported formats are those supported by torchaudio.load(). "
            "For example, wav and flac are supported. "
            "The sample rate has to be 16kHz."
        ),
    )

    parser.add_argument(
        "--beam-size",
        type=int,
        default=5,
        help="Used only when --method is beam_search",
    )

    return parser


def get_params() -> AttributeDict:
    params = AttributeDict(
        {
            "sample_rate": 16000,
            # parameters for conformer
            "feature_dim": 80,
            "encoder_out_dim": 512,
            "subsampling_factor": 4,
            "attention_dim": 512,
            "nhead": 8,
            "dim_feedforward": 2048,
            "num_encoder_layers": 12,
            "vgg_frontend": False,
            # decoder params
            "decoder_embedding_dim": 1024,
            "num_decoder_layers": 2,
            "decoder_hidden_dim": 512,
            "env_info": get_env_info(),
        }
    )
    return params


def get_encoder_model(params: AttributeDict):
    encoder = Conformer(
        num_features=params.feature_dim,
        output_dim=params.encoder_out_dim,
        subsampling_factor=params.subsampling_factor,
        d_model=params.attention_dim,
        nhead=params.nhead,
        dim_feedforward=params.dim_feedforward,
        num_encoder_layers=params.num_encoder_layers,
        vgg_frontend=params.vgg_frontend,
    )
    return encoder


def get_decoder_model(params: AttributeDict):
    decoder = Decoder(
        vocab_size=params.vocab_size,
        embedding_dim=params.decoder_embedding_dim,
        blank_id=params.blank_id,
        num_layers=params.num_decoder_layers,
        hidden_dim=params.decoder_hidden_dim,
        output_dim=params.encoder_out_dim,
    )
    return decoder


def get_joiner_model(params: AttributeDict):
    joiner = Joiner(
        input_dim=params.encoder_out_dim,
        output_dim=params.vocab_size,
    )
    return joiner


def get_transducer_model(params: AttributeDict):
    encoder = get_encoder_model(params)
    decoder = get_decoder_model(params)
    joiner = get_joiner_model(params)

    model = Transducer(
        encoder=encoder,
        decoder=decoder,
        joiner=joiner,
    )
    return model


def read_sound_files(
    filenames: List[str], expected_sample_rate: float
) -> List[torch.Tensor]:
    """Read a list of sound files into a list 1-D float32 torch tensors.
    Args:
      filenames:
        A list of sound filenames.
      expected_sample_rate:
        The expected sample rate of the sound files.
    Returns:
      Return a list of 1-D float32 torch tensors.
    """
    ans = []
    for f in filenames:
        wave, sample_rate = torchaudio.load(f)
        assert (
            sample_rate == expected_sample_rate
        ), f"expected sample rate: {expected_sample_rate}. Given: {sample_rate}"
        # We use only the first channel
        ans.append(wave[0])
    return ans


def main():
    parser = get_parser()
    args = parser.parse_args()

    params = get_params()

    params.update(vars(args))

    sp = spm.SentencePieceProcessor()
    sp.load(params.bpe_model)

    # <blk> is defined in local/train_bpe_model.py
    params.blank_id = sp.piece_to_id("<blk>")
    params.vocab_size = sp.get_piece_size()

    logging.info(f"{params}")

    device = torch.device("cpu")
    if torch.cuda.is_available():
        device = torch.device("cuda", 0)

    logging.info(f"device: {device}")

    logging.info("Creating model")
    model = get_transducer_model(params)

    checkpoint = torch.load(args.checkpoint, map_location="cpu")
    model.load_state_dict(checkpoint["model"], strict=False)
    model.to(device)
    model.eval()
    model.device = device

    logging.info("Constructing Fbank computer")
    opts = kaldifeat.FbankOptions()
    opts.device = device
    opts.frame_opts.dither = 0
    opts.frame_opts.snip_edges = False
    opts.frame_opts.samp_freq = params.sample_rate
    opts.mel_opts.num_bins = params.feature_dim

    fbank = kaldifeat.Fbank(opts)

    logging.info(f"Reading sound files: {params.sound_files}")
    waves = read_sound_files(
        filenames=params.sound_files, expected_sample_rate=params.sample_rate
    )
    waves = [w.to(device) for w in waves]

    logging.info("Decoding started")
    features = fbank(waves)
    feature_lengths = [f.size(0) for f in features]

    features = pad_sequence(features, batch_first=True, padding_value=math.log(1e-10))

    feature_lengths = torch.tensor(feature_lengths, device=device)

    with torch.no_grad():
        encoder_out, encoder_out_lens = model.encoder(
            x=features, x_lens=feature_lengths
        )

    num_waves = encoder_out.size(0)
    hyps = []
    for i in range(num_waves):
        # fmt: off
        encoder_out_i = encoder_out[i:i+1, :encoder_out_lens[i]]
        # fmt: on
        if params.method == "greedy_search":
            hyp = greedy_search(model=model, encoder_out=encoder_out_i)
        elif params.method == "beam_search":
            hyp = beam_search(
                model=model, encoder_out=encoder_out_i, beam=params.beam_size
            )
        else:
            raise ValueError(f"Unsupported method: {params.method}")

        hyps.append(sp.decode(hyp).split())

    s = "\n"
    for filename, hyp in zip(params.sound_files, hyps):
        words = " ".join(hyp)
        s += f"{filename}:\n{words}\n\n"
    logging.info(s)

    logging.info("Decoding Done")


if __name__ == "__main__":
    formatter = "%(asctime)s %(levelname)s [%(filename)s:%(lineno)d] %(message)s"

    logging.basicConfig(format=formatter, level=logging.INFO)
    main()
