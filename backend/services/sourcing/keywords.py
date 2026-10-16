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
    """Tokenize a listing title after Unicode compatibility normalization and lowercasing.

    Args:
        title: Listing-title string to Unicode-normalize and tokenize.

    Returns:
        List of title tokens preserving supported internal hyphens, dots and slashes.
    """
    return TOKEN.findall(unicodedata.normalize('NFKC', title).lower())


def bare(word):
    """Remove hyphens so model-name spellings compare consistently.

    'f-150' and 'f150' (or 'cr-v' and 'crv') are the same word.

    Args:
        word: Individual keyword/title token to normalize or evaluate.

    Returns:
        Word string with all hyphens removed.
    """
    return word.replace('-', '')


def phrase_key(phrase):
    """Normalize a phrase into lowercase tokens with hyphenless model forms.

    Args:
        phrase: Keyword phrase to normalize.

    Returns:
        Space-separated phrase comparison string.
    """
    return ' '.join(bare(w) for w in tokens(phrase))


def model_code(word):
    """Identify mixed letter/digit model tokens while rejecting quantity, unit and dimension specifications.

    Letter+digit model codes such as f150, rav4, 4runner, ps3; not specs or quantities.

    Args:
        word: Individual keyword/title token to normalize or evaluate.

    Returns:
        True for a supported model-code-like token, otherwise False.
    """
    word = bare(word)
    return (word.isalnum() and any(c.isalpha() for c in word) and any(c.isdigit() for c in word)
            and not re.search(r'\dx\d', word) and not UNIT.fullmatch(word))


def segments(title):
    """Split title text at separators so extracted phrases never span unrelated title sections.

    Args:
        title: Product listing title from which tokens or phrases are extracted.

    Returns:
        List of token lists; drops a final word cut off by a trailing ellipsis.
    """
    text = unicodedata.normalize('NFKC', title).lower().strip()
    parts = [words for words in (tokens(s) for s in SEGMENT.split(text)) if words]
    if text.endswith('...') and parts:
        parts[-1] = parts[-1][:-1]  # The final word was cut off ("Muran...").
    return parts


def model_codes(title):
    """Collect distinct potential model codes appearing in the listing title.

    Args:
        title: Product listing title from which tokens or phrases are extracted.

    Returns:
        Set of hyphenless model-code strings.
    """
    return {bare(w) for words in segments(title) for w in words if model_code(w)}


def singular(word):
    """Apply conservative plural normalization with explicit exceptions.

    Args:
        word: Individual keyword/title token to normalize or evaluate.

    Returns:
        Singularized word string, or the original word when no rule applies.
    """
    if word in PLURALS:
        return PLURALS[word]
    if len(word) > 3 and word.endswith('s') and not word.endswith(('ss', 'us', 'is')):
        return word[:-1]
    return word


def canonical(phrase):
    """Canonicalize plural, reordered, hyphenated and known synonym phrase variants.

    Args:
        phrase: Keyword phrase to normalize.

    Returns:
        Sorted tuple of normalized words suitable for equality and redundancy checks.
    """
    phrase = PHRASE_ALIASES.get(phrase_key(phrase), phrase)
    return tuple(sorted(singular(bare(w)) for w in tokens(phrase)))


def normalized_words(text):
    """Normalize phrase words while retaining order for safe literal containment checks.

    Space-padded singular word sequence for literal phrase containment checks.

    Args:
        text: Title or keyword string to normalize while preserving word order.

    Returns:
        Space-padded string of singularized, hyphenless words.
    """
    return ' ' + ' '.join(singular(bare(w)) for w in tokens(text)) + ' '


def redundant(left, right):
    """Check whether normalized phrases overlap by complete-word containment.

    Args:
        left: First keyword phrase to compare.
        right: Second keyword phrase to compare.

    Returns:
        True when either canonical word set is a subset of the other.
    """
    a, b = set(canonical(left)), set(canonical(right))
    return a <= b or b <= a


def extract(title, blocked=(), codes=frozenset(), unigram_allowlist=('oem',)):
    """Extract usable contiguous one-to-three-word phrases within title sections.

    Contiguous 1–3 word phrases. Model codes count only when listed in `codes`.
    Single words must be a model code or on the curated allowlist (makes/models, OEM).

    Args:
        title: Product listing title from which tokens or phrases are extracted.
        blocked: Phrases excluded from keyword extraction.
        codes: Approved hyphenless model codes allowed in extracted phrases.
        unigram_allowlist: Curated single-word phrases allowed in extraction.

    Returns:
        Set of literal phrases excluding blocked, generic, full-title and unapproved single-word candidates.
    """
    blocked = {phrase_key(p) for p in blocked}
    unigram_allowlist = {phrase_key(p) for p in unigram_allowlist}
    parts = segments(title)
    total = sum(len(words) for words in parts)

    def usable(word):
        """Check an extracted word against stop words, minimum letter length and approved model codes.

        Args:
            word: Individual keyword/title token to normalize or evaluate.

        Returns:
            True when the enclosing extraction may use this word.
        """
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
