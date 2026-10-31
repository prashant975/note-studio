"""Automatically pick the UPSC General Studies subject for a lecture/handout.

Scores keyword hits across the file name and the extracted slide/page text so
the UI never has to ask the user. Falls back to `polity` when nothing matches.
"""
from __future__ import annotations

import re

SUBJECT_KEYWORDS = {
    "polity": [
        "polity", "constitution", "constitutional", "article ", "amendment", "fundamental right",
        "directive principle", "dpsp", "parliament", "lok sabha", "rajya sabha", "judiciary",
        "supreme court", "high court", "president", "governor", "election commission", "federalism",
        "panchayat", "municipal", "preamble", "writ", "bill", "ordinance", "cag", "upsc article",
        "citizenship", "emergency provision", "schedule", "governance", "rti", "cabinet",
    ],
    "economy": [
        "economy", "economic", "gdp", "gnp", "inflation", "fiscal", "monetary", "rbi", "repo rate",
        "budget", "taxation", "gst", "banking", "npa", "balance of payment", "current account",
        "wpi", "cpi", "subsidy", "poverty", "unemployment", "planning commission", "niti aayog",
        "disinvestment", "fdi", "sensex", "nifty", "microfinance", "financial inclusion", "sez",
    ],
    "geography": [
        "geography", "geographical", "monsoon", "climate", "plate tectonic", "earthquake", "volcano",
        "river", "drainage", "soil", "vegetation", "latitude", "longitude", "atmosphere", "ocean current",
        "cyclone", "mountain", "plateau", "peninsula", "himalaya", "delta", "erosion", "rock", "mineral",
        "cartography", "map", "monsoon", "isotherm", "el nino", "coriolis",
    ],
    "ancient": [
        "ancient india", "indus valley", "harappan", "vedic", "mauryan", "ashoka", "gupta", "buddhism",
        "jainism", "mahajanapada", "chandragupta", "sangam", "megalithic", "chalcolithic", "kushana",
        "satavahana", "pallava", "harsha", "upanishad", "arthashastra",
    ],
    "medieval": [
        "medieval", "delhi sultanate", "mughal", "akbar", "aurangzeb", "babur", "slave dynasty",
        "khilji", "tughlaq", "lodi", "vijayanagar", "bahmani", "bhakti", "sufi", "maratha", "shivaji",
        "sher shah", "rajput", "chola",
    ],
    "modern": [
        "modern india", "british", "east india company", "revolt of 1857", "sepoy", "freedom struggle",
        "national movement", "gandhi", "nehru", "congress", "muslim league", "partition", "swadeshi",
        "non-cooperation", "civil disobedience", "quit india", "governor general", "viceroy", "plassey",
        "permanent settlement", "socio-religious reform", "moderates", "extremists", "home rule",
    ],
    "culture": [
        "art and culture", "art & culture", "architecture", "temple", "stupa", "painting", "sculpture",
        "classical dance", "bharatanatyam", "kathak", "music", "raga", "gharana", "folk", "handicraft",
        "unesco heritage", "festival", "literature", "philosophy", "manuscript", "gandhara", "dravidian",
        "nagara", "mural",
    ],
    "environment": [
        "environment", "ecology", "ecosystem", "biodiversity", "climate change", "global warming",
        "greenhouse", "conservation", "wildlife", "national park", "sanctuary", "biosphere", "wetland",
        "ramsar", "endemic", "endangered", "pollution", "carbon", "unfccc", "cop", "paris agreement",
        "food chain", "trophic", "sustainable", "afforestation", "coral", "tiger reserve",
    ],
    "science": [
        "science and technology", "science & tech", "biotechnology", "nanotechnology", "space",
        "isro", "satellite", "defence technology", "missile", "vaccine", "genome", "dna", "artificial intelligence",
        "quantum", "semiconductor", "nuclear", "reactor", "drdo", "gene editing", "crispr", "5g", "supercomputer",
        "stem cell", "biofuel",
    ],
    "ir": [
        "international relations", "foreign policy", "bilateral", "multilateral", "united nations",
        "unsc", "wto", "imf", "world bank", "brics", "g20", "saarc", "asean", "quad", "diplomacy",
        "treaty", "summit", "geopolitic", "indo-pacific", "neighbourhood", "sco", "nato", "sanction",
    ],
    "society": [
        "indian society", "social", "caste", "communalism", "secularism", "urbanisation", "urbanization",
        "population", "demographic", "migration", "gender", "women empowerment", "tribal", "poverty",
        "social justice", "vulnerable section", "regionalism", "diversity", "family", "globalisation and society",
    ],
    "security": [
        "internal security", "terrorism", "insurgency", "naxal", "left wing extremism", "cyber security",
        "money laundering", "border management", "organised crime", "radicalisation", "armed forces",
        "paramilitary", "intelligence agency", "money laundering", "counter-terror", "coastal security",
    ],
    "ethics": [
        "ethics", "integrity", "aptitude", "moral", "values", "attitude", "emotional intelligence",
        "probity", "conscience", "civil service value", "accountability", "case study", "dilemma",
        "corruption", "code of conduct", "foundational value", "empathy", "governance ethics",
    ],
}


def _score(text: str, keywords: list[str]) -> int:
    text = text.lower()
    return sum(len(re.findall(re.escape(k), text)) for k in keywords)


def detect_subject(filename: str = "", slide_text: str = "", default: str = "polity") -> str:
    """Return one of the UPSC subject keys (see SUBJECT_KEYWORDS).

    The file name is weighted heavily (it usually names the topic), with the
    slide/page text as a tie-breaker. Falls back to `default` when nothing hits.
    """
    name = (filename or "").lower()
    body = (slide_text or "").lower()
    scores = {}
    for subject, keywords in SUBJECT_KEYWORDS.items():
        scores[subject] = _score(name, keywords) * 5 + _score(body, keywords)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else default
