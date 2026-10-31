"""Per-subject guidance for UPSC note generation.

The note *structure* is shared across subjects (see prompts/base_*.txt). What
changes per subject is the emphasis — what a Prelims aspirant must memorise and
what a Mains aspirant must be able to argue. `SUBJECT_FOCUS` injects that
emphasis into the generation prompt (see prompt_builder.build_generation_prompt).

Kept as plain data so a non-programmer can tune the "quality" of any subject's
notes by editing the bullet lines below.
"""
from __future__ import annotations

from .config import SUBJECT_LABELS

# English focus blocks. Each is appended after the shared base template.
SUBJECT_FOCUS_EN = {
    "polity": (
        "Emphasise exact Constitutional Articles, Amendments, Schedules, and landmark "
        "Supreme Court judgments (name + year + one-line ratio). For every institution give "
        "composition, tenure, appointment, and powers. Distinguish clearly between what the "
        "Constitution says (Prelims) and debates on working/reform (Mains)."
    ),
    "economy": (
        "Define every term precisely and give the current value/rate where the source states it "
        "(repo rate, inflation, fiscal deficit, etc.). Name the responsible body (RBI, NITI Aayog, "
        "SEBI...). Capture schemes with launch year, ministry, and objective. For Mains, note the "
        "cause-effect chains and policy trade-offs (growth vs inflation, subsidy vs fiscal health)."
    ),
    "geography": (
        "Anchor every concept to a location and, wherever a slide has a map/diagram, insert it. "
        "Give exact names of ranges, rivers, straits, currents, soils, and their formation process. "
        "Link physical geography to human impact (disasters, agriculture, resources) for Mains."
    ),
    "ancient": (
        "Fix each development to a dynasty/period with dates and key rulers. Record sources "
        "(inscriptions, texts, foreign accounts), administrative and economic features, and "
        "art/architecture with site names. Note continuity and change across periods."
    ),
    "medieval": (
        "Track dynasties, key rulers, administrative systems (iqta, mansabdari, revenue), and "
        "socio-religious movements (Bhakti, Sufi) with proponents. Give architecture with patron "
        "and site. Note cultural synthesis and points of conflict."
    ),
    "modern": (
        "Keep a tight chronology of the freedom struggle with exact dates, sessions, acts, and "
        "personalities. For every movement give cause, leaders, methods, outcome, and significance. "
        "Note British policies (economic drain, land revenue) and their effects for Mains linkage."
    ),
    "culture": (
        "Name styles/schools with defining features and representative examples (temple style, "
        "dance form, painting school, music tradition). Give UNESCO/heritage tags and patrons. "
        "Trace evolution and regional variation; connect to syncretism and identity for Mains."
    ),
    "environment": (
        "Give exact names of species, protected areas, conventions (year + full form), and "
        "pollutants with thresholds. Capture ecological processes (food chains, cycles) accurately. "
        "For Mains, link to climate policy, sustainable development, and conservation debates."
    ),
    "science": (
        "Explain the concept in plain terms, then give the application, the Indian agency/mission "
        "(ISRO, DRDO, ICMR...), and recent developments named in the source. Keep facts current and "
        "exam-relevant. For Mains, note ethical, strategic, and socio-economic implications."
    ),
    "ir": (
        "For each relationship/grouping give members, year of formation, headquarters, and India's "
        "stake. Capture agreements, summits, and disputes with dates. For Mains, analyse strategic "
        "interests, convergences, and frictions rather than only listing events."
    ),
    "society": (
        "Define social concepts precisely and ground them in Indian data/examples (census, schemes, "
        "committees). For Mains, present multiple dimensions (causes, consequences, constitutional/"
        "policy responses) and balanced viewpoints on sensitive issues."
    ),
    "security": (
        "Name the threat, the agencies/laws/forces involved, and the affected regions precisely. "
        "Capture frameworks (acts, doctrines) with year. For Mains, analyse causes, linkages "
        "(development-security, cyber-financial), and multi-pronged responses."
    ),
    "ethics": (
        "Define each ethical term/theory crisply with a thinker where relevant, then give an "
        "illustrative example or civil-services application. For case studies, note the stakeholders, "
        "the dilemma, and the values in conflict. Keep it usable as answer-writing material."
    ),
    "history": (
        "Keep a clear chronology and tag every development to its period (ancient/medieval/modern) "
        "with dates, rulers or leaders, and sources of evidence. Give administrative, economic, and "
        "socio-religious features, plus art/architecture with site and patron. For Mains, bring out "
        "continuity and change across periods and the significance of each event, not just the event."
    ),
    "governance": (
        "Name the exact scheme, act, body, or committee with year, ministry, and objective, and give "
        "its structure and mandate (RTI, Lokpal, Citizens' Charter, e-governance missions, civil "
        "services reform). For Mains, analyse implementation gaps, accountability and transparency "
        "mechanisms, centre-state/local-body dimensions, and concrete reform measures."
    ),
    "current_affairs": (
        "Pin every item to its date, the issuing body, and the exact name/number (report, index, "
        "scheme, bill, summit, ranking). For Prelims, give the static concept the news hangs on. "
        "For Mains, give background, stakeholders, significance for India, criticisms, and a way "
        "forward. Flag anything time-sensitive that may have changed since the class."
    ),
}

# Hindi focus blocks. Shorter, but preserve the same intent.
SUBJECT_FOCUS_HI = {
    "polity": (
        "संविधान के सटीक अनुच्छेद, संशोधन, अनुसूचियाँ और महत्वपूर्ण सर्वोच्च न्यायालय के निर्णय "
        "(नाम + वर्ष) दें। प्रत्येक संस्था की संरचना, कार्यकाल, नियुक्ति और शक्तियाँ लिखें। संविधान "
        "क्या कहता है (प्रीलिम्स) और सुधार/कार्यप्रणाली की बहस (मेन्स) को अलग रखें।"
    ),
    "economy": (
        "हर पद की सटीक परिभाषा और स्रोत में दिया गया वर्तमान मान/दर दें। जिम्मेदार संस्था (RBI, "
        "NITI Aayog...) का नाम लें। योजनाओं का प्रारंभ वर्ष, मंत्रालय और उद्देश्य लिखें। मेन्स के लिए "
        "कारण-प्रभाव और नीतिगत संतुलन नोट करें।"
    ),
    "geography": (
        "प्रत्येक अवधारणा को स्थान से जोड़ें; मानचित्र/चित्र वाली स्लाइड को अवश्य डालें। पर्वत, नदी, "
        "जलधारा, मृदा के सटीक नाम व निर्माण प्रक्रिया दें। भौतिक भूगोल को मानव प्रभाव (आपदा, कृषि) से जोड़ें।"
    ),
    "ancient": (
        "प्रत्येक घटनाक्रम को राजवंश/काल, तिथियों और प्रमुख शासकों से जोड़ें। स्रोत, प्रशासनिक/आर्थिक "
        "विशेषताएँ और कला-स्थापत्य (स्थल सहित) लिखें।"
    ),
    "medieval": (
        "राजवंश, प्रमुख शासक, प्रशासनिक व्यवस्था (इक्ता, मनसबदारी) और भक्ति-सूफी आंदोलनों को प्रस्तावकों "
        "सहित दें। स्थापत्य को संरक्षक व स्थल सहित लिखें।"
    ),
    "modern": (
        "स्वतंत्रता संग्राम की सटीक तिथियों, अधिवेशनों, अधिनियमों और व्यक्तित्वों के साथ कालक्रम रखें। "
        "प्रत्येक आंदोलन का कारण, नेता, तरीका, परिणाम और महत्व दें।"
    ),
    "culture": (
        "शैलियों/संप्रदायों को विशेषताओं व उदाहरणों सहित नाम दें (मंदिर शैली, नृत्य, चित्रकला, संगीत)। "
        "UNESCO/धरोहर और संरक्षक जोड़ें; विकास व क्षेत्रीय विविधता दर्शाएँ।"
    ),
    "environment": (
        "प्रजातियों, संरक्षित क्षेत्रों, अभिसमयों (वर्ष सहित) और प्रदूषकों के सटीक नाम दें। पारिस्थितिक "
        "प्रक्रियाएँ सही रखें। मेन्स हेतु जलवायु नीति व सतत विकास से जोड़ें।"
    ),
    "science": (
        "अवधारणा को सरल भाषा में समझाएँ, फिर अनुप्रयोग, भारतीय एजेंसी/मिशन (ISRO, DRDO...) और स्रोत में "
        "उल्लिखित नवीनतम विकास दें। नैतिक/रणनीतिक निहितार्थ नोट करें।"
    ),
    "ir": (
        "प्रत्येक संबंध/समूह के सदस्य, गठन वर्ष, मुख्यालय और भारत का हित दें। समझौते व शिखर सम्मेलन तिथि "
        "सहित लिखें। मेन्स हेतु रणनीतिक हित व मतभेद विश्लेषित करें।"
    ),
    "society": (
        "सामाजिक अवधारणाओं की परिभाषा भारतीय आँकड़ों/उदाहरणों से दें। मेन्स हेतु कारण, परिणाम व नीतिगत "
        "प्रतिक्रियाओं के बहुआयामी व संतुलित दृष्टिकोण रखें।"
    ),
    "security": (
        "खतरा, संबंधित एजेंसियाँ/कानून/बल और प्रभावित क्षेत्र सटीक नाम सहित दें। ढाँचे (अधिनियम) वर्ष सहित "
        "लिखें। मेन्स हेतु कारण, अंतर्संबंध व बहुआयामी समाधान विश्लेषित करें।"
    ),
    "ethics": (
        "प्रत्येक नैतिक पद/सिद्धांत की स्पष्ट परिभाषा (विचारक सहित) और सिविल सेवा से जुड़ा उदाहरण दें। "
        "केस स्टडी में हितधारक, दुविधा और टकराते मूल्यों को नोट करें।"
    ),
    "history": (
        "प्रत्येक घटनाक्रम को उसके काल (प्राचीन/मध्यकालीन/आधुनिक), तिथियों और प्रमुख शासकों/नेताओं से "
        "जोड़कर स्पष्ट कालक्रम रखें। स्रोत, प्रशासनिक-आर्थिक विशेषताएँ और कला-स्थापत्य (स्थल व संरक्षक "
        "सहित) दें। मेन्स हेतु कालों में निरंतरता, परिवर्तन और महत्व दर्शाएँ।"
    ),
    "governance": (
        "योजना, अधिनियम, संस्था या समिति का सटीक नाम वर्ष, मंत्रालय और उद्देश्य सहित दें (RTI, लोकपाल, "
        "नागरिक चार्टर, ई-गवर्नेंस, सिविल सेवा सुधार)। मेन्स हेतु क्रियान्वयन की कमियाँ, पारदर्शिता व "
        "जवाबदेही तंत्र, केंद्र-राज्य/स्थानीय आयाम और ठोस सुधार सुझाएँ।"
    ),
    "current_affairs": (
        "प्रत्येक विषय को तिथि, संबंधित संस्था और सटीक नाम/संख्या (रिपोर्ट, सूचकांक, योजना, विधेयक, "
        "शिखर सम्मेलन) सहित लिखें। प्रीलिम्स हेतु उससे जुड़ी स्थैतिक अवधारणा दें। मेन्स हेतु पृष्ठभूमि, "
        "हितधारक, भारत के लिए महत्व, आलोचना और आगे की राह दें। समयसापेक्ष तथ्यों को चिह्नित करें।"
    ),
}


def subject_label(subject: str) -> str:
    """Human-readable subject name for the notes cover / UI."""
    return SUBJECT_LABELS.get((subject or "").lower(), (subject or "").title())


def subject_focus(subject: str, language_code: str) -> str:
    """Return the per-subject emphasis block for the given language ('en'/'hi')."""
    # Hindi has a translated focus table; Marathi does not. English focus
    # text under a Marathi output lock still yields Marathi notes, whereas
    # HINDI focus text invites Hindi ones — so Marathi takes the English table.
    from .languages import normalise

    table = SUBJECT_FOCUS_HI if normalise(language_code) == "hi" else SUBJECT_FOCUS_EN
    return table.get((subject or "").lower(), "")
