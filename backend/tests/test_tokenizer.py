from app.rag.tokenizer import TOKENIZER_VERSION, tokenize, unique_terms


def test_identifier_kept_and_split():
    tokens = tokenize("getUserName")
    assert "getusername" in tokens
    assert {"get", "user", "name"} <= set(tokens)


def test_snake_case_split():
    for text, pieces in [
        ("snake_case_id", {"snake", "case", "id"}),
        ("sha256_hash", {"sha256", "hash"}),
    ]:
        tokens = tokenize(text)
        assert pieces <= set(tokens)
        assert text in tokens  # the full identifier is kept as well


def test_case_and_punctuation_lowered():
    assert tokenize("Hello, WORLD!") == ["hello", "world"]


def test_cjk_bigrams_and_single_char():
    assert tokenize("知识库") == ["知识", "识库"]
    assert tokenize("查") == ["查"]
    tokens = tokenize("检索 RAG 知识")
    assert "检索" in tokens and "rag" in tokens and "知识" in tokens


def test_unique_terms():
    assert unique_terms("aa aa bb") == {"aa", "bb"}
    assert TOKENIZER_VERSION == "zh-en.v1"
