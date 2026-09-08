"""U3 banned phrases, U4 question count, U5 menu count."""
import pytest

from journal_ai.gates.style import (BANNED_PHRASES, StyleGate, count_menu_options, count_questions,
                                    count_sentences, find_banned_phrases)


@pytest.mark.parametrize("phrase", BANNED_PHRASES)
def test_u3_each_banned_phrase_caught(phrase):
    assert find_banned_phrases(f"Maybe you could {phrase} here.")
    assert find_banned_phrases(f"MAYBE YOU COULD {phrase.upper()}.")
    assert find_banned_phrases(f"{phrase.capitalize()}, they said.")


def test_u3_variants_with_punctuation_and_curly_apostrophes():
    assert find_banned_phrases("Lean, into it")
    assert find_banned_phrases("how far you’ve come")
    assert find_banned_phrases("That was brave of you to write.")


def test_u3_control_corpus_no_false_positives():
    control = [
        "I read that. You wrote about the meeting and then about dinner.",
        "You said it was fine, and then said it again two lines later.",
        "Do you want to stay with that, or leave it for tonight?",
        "The important thing you wrote was about your brother.",  # 'important' not applied to journaling
        "The leaning tower, the healed wound in the story, the power outage.",
    ]
    for c in control:
        assert find_banned_phrases(c) == [], c


def test_u3_conditional_praise_applied_to_journaling():
    assert find_banned_phrases("Writing this down is a healthy habit.")
    assert find_banned_phrases("It's important that you journal.")
    assert not find_banned_phrases("The healthy option on the menu was fish.")


@pytest.mark.parametrize("text,n", [
    ("I read that.", 0),
    ("What made today different?", 1),
    ("What happened? And then what?", 2),
    ('You wrote "why do I do this?" twice. What do you make of that?', 1),
    ("You asked “is this it?” and then stopped.", 0),
    ("Really??", 1),
])
def test_u4_question_count(text, n):
    assert count_questions(text) == n


@pytest.mark.parametrize("text,n", [
    ("I can listen, reflect, or push back — up to you.", 3),
    ("Would you like me to listen or reflect?", 2),
    ("- listen\n- reflect\n- challenge\n- decide", 4),
    ("1. Listen\n2. Reflect\n3. Challenge", 3),
    ("I read that. You mentioned your sister and the house.", 0),
    ("You wrote about work, your sister, and the car.", 0),
])
def test_u5_menu_count(text, n):
    assert count_menu_options(text) == n


def test_sentence_count_and_cap():
    text = " ".join(f"Sentence number {i}." for i in range(1, 11))
    assert count_sentences(text) == 10
    res = StyleGate(hard_sentence_cap=8).check(text)
    assert not res.ok and any(v["type"] == "sentence_cap" for v in res.violations)
    assert "Rewrite" in res.tightening_instruction()


def test_gate_forbid_menu_for_you_decide():
    res = StyleGate().check("I could reflect or I could ask a question — which would help?", forbid_menu=True)
    assert any(v["type"] == "menu_forbidden" for v in res.violations)
    ok = StyleGate().check("You keep coming back to the word allowed. I'll sit with that with you.", forbid_menu=True)
    assert ok.ok, ok.violations


def test_gate_zero_questions_when_hold():
    res = StyleGate().check("That sounds like it landed. What now?", max_questions=0)
    assert any(v["type"] == "question_count" for v in res.violations)
