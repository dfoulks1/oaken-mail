# SPDX-License-Identifier: Apache-2.0
import time
from pathlib import Path

import pytest

from oaken.core.errors import ConfigError
from oaken.core.settings import MailSettings
from oaken.mail import EstimateTokenizer, FileTokenizer, clean, load_tokenizer, parse


def make_tokenizer_file(tmp_path: Path) -> Path:
    """A tiny word-level tokenizer built in-process: no download, no model file in the repo."""
    from tokenizers import Tokenizer, models, pre_tokenizers

    vocab = {"[UNK]": 0, "hello": 1, "there": 2, "world": 3}
    tok = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))  # noqa: S106
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    path = tmp_path / "tokenizer.json"
    tok.save(str(path))
    return path


def test_the_estimate_is_deterministic_and_roughly_word_sized() -> None:
    t = EstimateTokenizer()
    assert t.count("") == 0 and not t.exact
    assert t.count("hello world") == 2
    assert t.count("hello, world!") == 4
    assert t.count("antidisestablishmentarianism") > 3
    assert t.count("会議は木曜日") == 6
    assert t.count("hello world") == t.count("hello world")


def test_a_file_tokenizer_gives_exact_counts(tmp_path: Path) -> None:
    t = FileTokenizer(make_tokenizer_file(tmp_path))
    assert t.exact and t.name == "file:tokenizer.json"
    assert t.count("hello there world") == 3
    assert t.count("hello unknown") == 2


def test_load_tokenizer_follows_settings(tmp_path: Path) -> None:
    assert isinstance(load_tokenizer(MailSettings()), EstimateTokenizer)
    path = make_tokenizer_file(tmp_path)
    assert isinstance(load_tokenizer(MailSettings(tokenizer_file=str(path))), FileTokenizer)


def test_a_missing_or_broken_tokenizer_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        load_tokenizer(MailSettings(tokenizer_file=str(tmp_path / "nope.json")))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ConfigError, match="cannot load"):
        load_tokenizer(MailSettings(tokenizer_file=str(bad)))


def test_clean_reports_counts_from_the_chosen_tokenizer(tmp_path: Path) -> None:
    t = FileTokenizer(make_tokenizer_file(tmp_path))
    parsed = parse(b"From: a@example.com\n\nhello there\n\n-- \nhello world\n")
    c = clean(parsed, tokenizer=t)
    assert c.tokenizer == "file:tokenizer.json"
    assert (c.tokens_in, c.tokens_out) == (5, 2)


def test_counting_is_cheap() -> None:
    text = "The quarterly planning review moves to Thursday. " * 400  # about 20 KB
    t = EstimateTokenizer()
    start = time.perf_counter()
    for _ in range(20):
        t.count(text)
    per_call = (time.perf_counter() - start) / 20
    assert per_call < 0.05  # a generous bound; it is typically a millisecond or two
