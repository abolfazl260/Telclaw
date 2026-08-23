from collection.crawler import (
    MIN_MESSAGE_WORDS,
    _has_minimum_message_text,
    _word_count,
)


def test_messages_below_ten_words_are_rejected():
    assert _word_count("one two three four five six seven eight nine") == 9
    assert not _has_minimum_message_text("one two three four five six seven eight nine")


def test_exactly_ten_words_are_kept():
    text = "one two three four five six seven eight nine ten"
    assert _word_count(text) == MIN_MESSAGE_WORDS
    assert _has_minimum_message_text(text)


def test_whitespace_is_normalized_before_counting():
    text = "one   two\nthree\tfour five six seven eight nine ten"
    assert _word_count(text) == 10
    assert _has_minimum_message_text(text)


def test_empty_or_whitespace_only_messages_are_rejected():
    assert _word_count("") == 0
    assert _word_count("   \n\t  ") == 0
    assert not _has_minimum_message_text("")
    assert not _has_minimum_message_text("   \n\t  ")
