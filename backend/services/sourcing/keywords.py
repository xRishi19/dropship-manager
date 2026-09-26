"""Literal contiguous title phrases and conservative redundancy checks."""
import re
import unicodedata

STOP = set('a an the for with and or of to in on by from fits fit compatible replacement new used genuine quality premium universal original set kit pack pcs piece pieces free shipping fast pair black white red blue'.split())
# Replacement is useful as an adjective, so preserve it inside phrases.
STOP.remove('replacement')
GENERIC = set('replacement parts part accessory accessories product products item items tool tools'.split())
TOKEN = re.compile(r"[a-z0-9]+(?:[-./][a-z0-9]+)*", re.I)
# Title separators; phrases never bridge them ("Lug Nuts | Heavy Duty").
SEGMENT = re.compile(r"[|,;()\[\]]|\s-\s")
# Quantities, sizes and units ("2pcs", "10ft", "15ml", "6-pack") are not product words.
UNIT = re.compile(r"\d+(?:pcs?|pk|packs?|ct|count|ft|feet|in|inch|inches|mm|cm|m|yd|ml|l|oz|mg|g|kg|lbs?|v|w|ah|mah|k|gb|tb|qt|gal|x|pieces?|sets?|pairs?)")
PLURALS = {'accessories': 'accessory', 'batteries': 'battery'}
# Common sourcing synonyms that should not consume separate daily slots.
PHRASE_ALIASES = {'tow hitch': 'trailer hitch'}


def tokens(title):
    return TOKEN.findall(unicodedata.normalize('NFKC', title).lower())


def bare(word):
    """'f-150' and 'f150' (or 'cr-v' and 'crv') are the same word."""
    return word.replace('-', '')


def phrase_key(phrase):
    return ' '.join(bare(w) for w in tokens(phrase))


def model_code(word):
    """Letter+digit model codes such as f150, rav4, 4runner, ps3; not specs or quantities."""
    word = bare(word)
    return (word.isalnum() and any(c.isalpha() for c in word) and any(c.isdigit() for c in word)
            and not re.search(r'\dx\d', word) and not UNIT.fullmatch(word))


def segments(title):
    text = unicodedata.normalize('NFKC', title).lower().strip()
    parts = [words for words in (tokens(s) for s in SEGMENT.split(text)) if words]
    if text.endswith('...') and parts:
        parts[-1] = parts[-1][:-1]  # The final word was cut off ("Muran...").
    return parts


def model_codes(title):
    return {bare(w) for words in segments(title) for w in words if model_code(w)}


def singular(word):
    if word in PLURALS:
        return PLURALS[word]
    if len(word) > 3 and word.endswith('s') and not word.endswith(('ss', 'us', 'is')):
        return word[:-1]
    return word


def canonical(phrase):
    phrase = PHRASE_ALIASES.get(phrase_key(phrase), phrase)
    return tuple(sorted(singular(bare(w)) for w in tokens(phrase)))


def normalized_words(text):
    """Space-padded singular word sequence for literal phrase containment checks."""
    return ' ' + ' '.join(singular(bare(w)) for w in tokens(text)) + ' '


def redundant(left, right):
    a, b = set(canonical(left)), set(canonical(right))
    return a <= b or b <= a


def extract(title, blocked=(), codes=frozenset(), unigram_allowlist=('oem',)):
    """Contiguous 1–3 word phrases. Model codes count only when listed in `codes`.
    Single words must be a model code or on the curated allowlist (makes/models, OEM)."""
    blocked = {phrase_key(p) for p in blocked}
    unigram_allowlist = {phrase_key(p) for p in unigram_allowlist}
    parts = segments(title)
    total = sum(len(words) for words in parts)

    def usable(word):
        if word in STOP:
            return False
        if bare(word).isalpha():
            return len(bare(word)) >= 2
        return model_code(word) and bare(word) in codes

    result = set()
    for words in parts:
        for size in (1, 2, 3):
            for index in range(len(words) - size + 1):
                group = words[index:index + size]
                if not all(usable(word) for word in group):
                    continue
                phrase = ' '.join(group)
                key = phrase_key(phrase)
                if size == 1 and key not in unigram_allowlist and not model_code(phrase):
                    continue
                if all(word in GENERIC for word in group) or key in blocked:
                    continue
                if size == total:  # Do not emit full listing titles.
                    continue
                result.add(phrase)
    return result
