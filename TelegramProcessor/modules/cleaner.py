import re

def remove_emojis(text):
    if not isinstance(text, str):
        return ""
    return re.sub("[\U00010000-\U0010ffff]", "", text)

def normalize_text(text):
    text = remove_emojis(text).lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def clean_dataframe(df):
    df = df.copy()
    df = df[df["text"].notna()]
    df = df[df["text"].str.strip() != ""]
    df["original_text"] = df["text"]
    df["normalized_text"] = df["text"].apply(normalize_text)
    return df
