"""
Anatomy mnemonics section for Ghana Nursing Mnemonics guide.
Attribution: adapted from common anatomy study aids (e.g. James Lamberg–style lists).
"""

_CRANIAL_NERVE_FIBERS_HTML = """
<div class="anat-rich">
  <ol class="anat-steps">
    <li class="anat-step anat-step--danger"><strong>Step 1 —</strong> Write <span class="anat-mono">SE SA VE VA SA VE VA SE</span>
      (“SE Save VA Save Vase”). <em class="anat-sub">a.</em> Keep spaces between the two letters.</li>
    <li class="anat-step anat-step--primary"><strong>Step 2 —</strong> Write four <strong>S</strong> and four <strong>G</strong> before each pair of letters.
      <em class="anat-sub">a.</em> Place a <strong>p</strong> after the 2nd <strong>G</strong> → <span class="anat-mono">GVEp</span>.</li>
    <li class="anat-step anat-step--success"><strong>Step 3 —</strong> Phone number: <strong>251-5303</strong> (filled along the columns).</li>
    <li class="anat-step anat-step--warning"><strong>Step 4 —</strong> “Goal posts”: add <strong>7, 9, 10</strong> and an <strong>11</strong> in the last column.</li>
    <li class="anat-step anat-step--dark"><strong>Step 5 —</strong> Remaining digits use memory / simple multiplication (black numbers).</li>
  </ol>

  <figure class="anat-figure">
    <figcaption>Reference diagram</figcaption>
    <img
      src="/images/mnemonics/cranial-nerve-fibers-phone.png"
      alt="Cranial nerve fiber types mnemonic diagram"
      width="720"
      height="480"
      loading="lazy"
      decoding="async"
      class="anat-figure__img"
    />
  </figure>

  <div class="anat-table-scroll">
    <table class="anat-grid" aria-label="Cranial nerve numbers by fiber column">
      <thead>
        <tr>
          <th><span class="anat-hdr anat-hdr--g">SS</span><span class="anat-hdr anat-hdr--r">E</span></th>
          <th><span class="anat-hdr anat-hdr--g">SS</span><span class="anat-hdr anat-hdr--r">A</span></th>
          <th><span class="anat-hdr anat-hdr--g">S</span><span class="anat-hdr anat-hdr--r">VE</span></th>
          <th><span class="anat-hdr anat-hdr--g">S</span><span class="anat-hdr anat-hdr--r">VA</span></th>
          <th><span class="anat-hdr anat-hdr--g">G</span><span class="anat-hdr anat-hdr--r">SA</span></th>
          <th><span class="anat-hdr anat-hdr--g">G</span><span class="anat-hdr anat-hdr--r">VE</span><span class="anat-hdr anat-hdr--g">p</span></th>
          <th><span class="anat-hdr anat-hdr--g">G</span><span class="anat-hdr anat-hdr--r">VA</span></th>
          <th><span class="anat-hdr anat-hdr--g">G</span><span class="anat-hdr anat-hdr--g">S</span><span class="anat-hdr anat-hdr--r">E</span></th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td class="anat-cell anat-cell--k">8</td>
          <td class="anat-cell anat-cell--ph">2</td>
          <td class="anat-cell anat-cell--ph">5</td>
          <td class="anat-cell anat-cell--ph">1</td>
          <td class="anat-cell anat-cell--ph">5</td>
          <td class="anat-cell anat-cell--ph">3</td>
          <td class="anat-cell anat-cell--ph">0</td>
          <td class="anat-cell anat-cell--ph">3</td>
        </tr>
        <tr>
          <td class="anat-cell anat-cell--empty"></td>
          <td class="anat-cell anat-cell--k">8</td>
          <td class="anat-cell anat-cell--go">7</td>
          <td class="anat-cell anat-cell--go">7</td>
          <td class="anat-cell anat-cell--go">7</td>
          <td class="anat-cell anat-cell--go">7</td>
          <td class="anat-cell anat-cell--go">7</td>
          <td class="anat-cell anat-cell--k">4</td>
        </tr>
        <tr>
          <td class="anat-cell anat-cell--empty"></td>
          <td class="anat-cell anat-cell--empty"></td>
          <td class="anat-cell anat-cell--go">9</td>
          <td class="anat-cell anat-cell--go">9</td>
          <td class="anat-cell anat-cell--go">9</td>
          <td class="anat-cell anat-cell--go">9</td>
          <td class="anat-cell anat-cell--go">9</td>
          <td class="anat-cell anat-cell--k">12</td>
        </tr>
        <tr>
          <td class="anat-cell anat-cell--empty"></td>
          <td class="anat-cell anat-cell--empty"></td>
          <td class="anat-cell anat-cell--go">10</td>
          <td class="anat-cell anat-cell--go">10</td>
          <td class="anat-cell anat-cell--go">10</td>
          <td class="anat-cell anat-cell--go">10</td>
          <td class="anat-cell anat-cell--go">10</td>
          <td class="anat-cell anat-cell--k">6</td>
        </tr>
        <tr>
          <td class="anat-cell anat-cell--empty"></td>
          <td class="anat-cell anat-cell--empty"></td>
          <td class="anat-cell anat-cell--empty"></td>
          <td class="anat-cell anat-cell--empty"></td>
          <td class="anat-cell anat-cell--empty"></td>
          <td class="anat-cell anat-cell--empty"></td>
          <td class="anat-cell anat-cell--empty"></td>
          <td class="anat-cell anat-cell--go">11</td>
        </tr>
      </tbody>
    </table>
  </div>
  <p class="anat-legend"><span class="anat-leg anat-leg--ph">Green</span> = phone line ·
  <span class="anat-leg anat-leg--go">Orange</span> = goal-post line ·
  <span class="anat-leg anat-leg--k">Black</span> = remainder / anchors</p>
</div>
"""


def _rows(lines):
    out = []
    for i, line in enumerate(lines, start=1):
        t = str(line or "").strip()
        if not t:
            continue
        out.append({"letter": str(i), "meaning": t, "explanation": ""})
    return out


def _card(title, code, lines, extra=""):
    blob = " ".join(str(x) for x in lines) + " " + title + " " + code + " " + extra
    return {
        "title": title,
        "code": code,
        "rows": _rows(lines),
        "extra_search": blob.lower(),
    }


_ANATOMY_DATA = [
    _card(
        "Deep Muscles of the Back",
        "I Love Spaghetti – Some More Ragu",
        [
            "\"I Love Spaghetti – Some More Ragu\": Iliocostalis, Longissimus, Spinalis – Semispinalis, Multifidus, Rotatores.",
        ],
    ),
    _card(
        "Brachial Plexus — levels",
        "Really Thirsty? Drink Cold Beer",
        [
            "Roots, Trunks, Divisions, Cords, Branches.",
            "\"Really Thirsty? Drink Cold Beer\" or \"Randy Travis Drinks Cold Beers.\"",
        ],
    ),
    _card(
        "Brachial Plexus — terminal nerves",
        "MARMU",
        [
            "Musculocutaneous, Axillary, Radial, Median, Ulnar.",
        ],
    ),
    _card(
        "Muscles Inserting into Humerus",
        "A lady between two majors",
        [
            "Pectoralis major → lateral lip of bicipital groove; Teres major → medial lip; Latissimus dorsi → floor of groove. "
            '"Lati" between two "majors."',
        ],
    ),
    _card(
        "Carpal Bones (proximal → distal, lateral → medial)",
        "Some Lovers Try Positions That They Can't Handle",
        [
            "Proximal row: Scaphoid, Lunate, Triquetrum, Pisiform.",
            "Distal row: Trapezium, Trapezoid, Capitate, Hamate.",
            "TrapeziUM at the thUMb · TrapeziOID is inSIDE.",
            "Flexor carpi superficialis splits to let flexor digitorum profundus pass through.",
        ],
    ),
    _card(
        "Radial & median innervation highlights",
        "BEST · 2LOAF",
        [
            "Radial — \"BEST\": Brachioradialis, Extensors, Supinator, Triceps.",
            "Median — \"2LOAF\": Lateral 2 lumbricals, Opponens pollicis, Abductor pollicis brevis, Flexor pollicis brevis.",
        ],
    ),
    _card(
        "Rotator Cuff",
        "SITS · transverse ligament · cubital fossa",
        [
            "SITS: Supraspinatus, Infraspinatus, Teres minor, Subscapularis (3 greater tubercle, 1 lesser).",
            "Transverse scapular ligament — Army over (bridge), Navy under (bridge): artery over, nerve under.",
            'Cubital fossa (lateral → medial) — "TAN": Tendon (biceps), Artery (brachial), Nerve (median).',
        ],
    ),
    _card(
        "Hand intrinsic actions & palmar row",
        "PAD · DAB · All For One…",
        [
            '"PAD": palmar interossei ADduct.',
            '"DAB": dorsal interossei ABduct.',
            '"All For One And One For All" (medial → lateral): Abductor digiti minimi, Flexor digiti minimi, Opponens digiti minimi, Adductor pollicis, Opponens pollicis, Flexor pollicis brevis, Abductor pollicis brevis.',
        ],
    ),
    _card(
        "Axillary & thoracoacromial branches",
        "Save The Lions… · California Police Department",
        [
            '"Save The Lions And Protect Species": Supreme/Superior thoracic, Thoracoacromial, Lateral thoracic, Anterior circumflex humeral, Posterior circumflex humeral, Subscapular (thoracodorsal + circumflex scapular).',
            '"CAlifornia Police Department": Clavicular, Acromial, Pectoral, Deltoid (branches of thoracoacromial).',
            '"Lateral is less, medial is more": lateral pectoral nerve → through major only; medial pectoral → through major and minor.',
        ],
    ),
    _card(
        "Leg — deep posterior & medial ankle",
        "Down The Hatch · Tom, Dick, And Very Nervous Harry",
        [
            'Deep posterior compartment — "Down The Hatch": flexor Digitorum longus, Tibialis posterior, flexor Hallucis longus.',
            'Medial malleolus (anterior → posterior) — "Tom, Dick, And Very Nervous Harry": Tibialis posterior; flexor Digitorum longus; posterior tibial Artery; posterior tibial Vein; tibial Nerve; flexor Hallucis longus.',
            'Pes anserinus — "SGT FOS": Sartorius (femoral), Gracilis (obturator), Semitendinosus (sciatic).',
        ],
    ),
    _card(
        "Femoral triangle & anterior leg",
        "So I May Always Love Sally · NAVEL · THANDP",
        [
            'Boundaries — "So I May Always Love Sally": Superiorly – inguinal ligament; Medially – Adductor longus; Laterally – Sartorius.',
            'Contents (lateral → medial) — "NAVEL": Nerve, Artery, Vein, Empty space, Lymphatics.',
            'Anterior compartment — "The Hospitals Are Not Dirty Places": Tibialis anterior; extensor Hallucis longus; anterior tibial Artery; deep fibular Nerve; extensor Digitorum longus; Peroneus tertius.',
        ],
    ),
    _card(
        "Knee & leg nerves",
        "PAMs ApPLes · ATM · FED / TIP",
        [
            'Cruciate paths — "PAMs ApPLes": Posterior [passes] Anteriorly [inserts] Medially; Anterior [passes] Posteriorly [inserts] Laterally.',
            'Unhappy triad (US football) — "ATM": ACL, Tibial (medial) collateral, Medial meniscus.',
            '"FED": common Fibular nerve Evers and Dorsiflexes.',
            '"TIP": Tibial Inverts and Plantarflexes.',
        ],
    ),
    _card(
        "Foot — plantar layers & tunnels",
        "AFA 222 FAF · Tiny Dogs Are Not Hunters",
        [
            'Mnemonic "AFA 222 FAF" for plantar muscle layering (layers 1–3 as in standard texts).',
            'Tarsal tunnel (superior → inferior) — "Tiny Dogs Are Not Hunters": Tibialis posterior; flexor Digitorum; posterior tibial Artery; tibial Nerve; flexor Hallucis longus.',
            'Inguinal canal walls — "2MALT": roof (2 muscles), anterior (2 aponeuroses), floor (2 ligaments), posterior (2 Ts).',
        ],
    ),
    _card(
        "Tarsal bones & hip rotators",
        "Traverse City… · P-GO-GO-Q · VAN",
        [
            'Tarsals — "Traverse City (is) Noted (for) MIchigan\'s Lovely Cherries": Talus & Calcaneus; Navicular; Medial/Intermediate cuneiforms; Lateral cuneiform; Cuboid.',
            'Hip lateral rotators (greater trochanter) — "P-GO-GO-Q": Piriformis, Gemellus superior, Obturator internus, Gemellus inferior, Obturator externus, Quadratus femoris.',
            'Ribs / neck / sublingual hiatus (sup→inf, med→lat) — "VAN": Vein, Artery, Nerve.',
        ],
    ),
    _card(
        "Thorax — vagus, lung vessels, diaphragm",
        "Not Left Behind · RALS · I 8 10 Eggs At 12",
        [
            'Vagus into thorax — "Not Left Behind": left recurrent anterior (not behind); right posterior.',
            'Lung vessels — "RALS": Right lung Artery anterior to bronchus; Left lung Artery Superior to bronchus.',
            'Diaphragm piercings — "I 8 10 Eggs At 12": T8 IVC; T10 Esophagus; T12 Aorta.',
        ],
    ),
    _card(
        "Heart valves & spinal mnemonics",
        "RAT LAMB · C3-4-5 · C5-6-7",
        [
            'Valves — "RAT, LAMB" / "LAB RAT" (tricuspid right, mitral left).',
            'Spinal nerves — "C3-4-5 keeps the diaphragm alive (phrenic)"; "C5-6-7 raise your arms to heaven" (long thoracic to serratus anterior).',
        ],
    ),
    _card(
        "Pelvic / perineum highlights",
        "Some Dang Englishman… · S2,3,4 · ABC'S",
        [
            'Scrotum layers — "Some Dang Englishman Called It The Testis": Skin, Dartos, External spermatic, Cremaster, Internal spermatic, Tunica vaginalis, Testis.',
            'Penile innervation — "S2,3,4 keep the penis off the floor" (pudendal branches).',
            'Erection / emission / ejaculation — Point, Shoot, Score (para/sympathetic/somatic overview).',
            'Aortic arch — "ABC\'S" / "Boston College Stinks": brachiocephalic trunk, left common carotid, left subclavian.',
        ],
    ),
    _card(
        "Abdomen — portal triad & gut path",
        "DAV · GQ · Dow Jones…",
        [
            'Portal triad — "DAV": Duct, Artery, Vein.',
            'Liver — "GQ": Gallbladder beside Quadrate lobe.',
            'Gut path after stomach — "Dow Jones Industrial Climbing Average Closing Stock Report": Duodenum → Jejunum → Ileum → Cecum → Appendix → ascending/transverse Colon → Sigmoid → Rectum.',
            'Spleen — "1,3,5,7,9,11" sizing / rib reminder (classic exam trivia).',
            'Thoracic duct — "The duck is between two gooses" (azygos & esophagus).',
        ],
    ),
    _card(
        "Skin & head / face bones",
        "Brent Spiner… · Never Call Me Needle Nose · SCALP",
        [
            'Epidermis — "Brent Spiner Gained Lieutenant Commander": Basale, Spinosum, Granulosum, Lucidum, Corneum.',
            'Nasal cavity — "Never Call Me Needle Nose!": external Nares, Conchae, Meatuses, internal Nares, Nasopharynx.',
            'Scalp — "SCALP": Skin, Connective tissue, Aponeurosis, Loose areolar, Pericranium.',
            'Horner — "SPAM"; Bell palsy — "BELL\'S Palsy" mnemonic keywords.',
        ],
    ),
    _card(
        "Cranial bones",
        "Old People From Texas Eat Spiders",
        [
            'OPFTES: Occipital, Parietal, Frontal, Temporal, Ethmoid, Sphenoid.',
        ],
    ),
    _card(
        "Cranial nerves — rhyme & S/M/B",
        "Oh Oh Oh To Touch And Feel…",
        [
            "I On (Olfactory) — Some (S). II Old (Optic) — Say (S). III Olympus (Oculomotor) — Marry (M).",
            "IV Towering (Trochlear) — Money (M). V Tops (Trigeminal) — But (Both). VI A (Abducens) — My (M).",
            "VII Finn (Facial) — Brother (B). VIII And (Auditory) — Says (S). IX German (Glossopharyngeal) — Big (B).",
            "X Viewed (Vagus) — Bras (B). XI Astounding (Accessory) — Matter (M). XII Hops (Hypoglossal) — More (M).",
            '"You have 1 Nose & 2 Eyes": CN I smell · CN II sight.',
        ],
    ),
    _card(
        "Extraocular muscles & subclavian",
        "LR6 · SO4 · rest3 · VT is Cold",
        [
            'Extraocular — "LR6 – SO4 – rest3" / "(SO4LR6)3": lateral rectus VI; superior oblique IV; others mostly III.',
            'Subclavian branches — "VT is Cold": Vertebral, Thyrocervical trunk, Costocervical trunk.',
        ],
    ),
    _card(
        "Branches & sensory exits",
        "Please, To Zanzibar… · Standing Room Only",
        [
            'External carotid — "Some Anatomists Like Freaking Out Poor Medical Students" (classic branch list).',
            'Internal jugular (inf→sup) — "Medical Schools Let Confident People In".',
            'Facial nerve branches — "Please, To Zanzibar By Motor Car" / "Ten Zombies Bought My Car".',
            'V3 exit skull — "Standing Room Only": superior orbital fissure (V1), foramen Rotundum (V2), foramen Ovale (V3).',
        ],
    ),
    _card(
        "Ansa cervicalis & cervical plexus",
        "GHost THought… · GLAST",
        [
            'Ansa cervicalis — "GHost THought SOmeone STupid SHot Irene": roots to infrahyoid muscles (standard list).',
            'Cervical plexus — "GLAST": Great auricular, Lesser occipital, Accessory (between L & S), Supraclavicular, Transverse cervical.',
            'V3 muscles — "M.D. My T.V." (mastication, digastric anterior, mylohyoid, tensor tympani, tensor veli palatini).',
            'V3 sensory branches — "Buccaneers Are Inferior Linguists".',
        ],
    ),
    _card(
        "Lacrimal nerve course (8 L story)",
        "Eight L's",
        [
            "Lateral wall of orbit above lateral rectus → communicating branch joins → lacrimal gland → lateral upper eyelid (classic storytelling mnemonic).",
        ],
    ),
]

_fib = {
    "title": "Cranial Nerve Fibers (Phone Number)",
    "code": "SSE / SSA / SVE / SVA / GSA / GVEp / GVA / GSE",
    "body_html": _CRANIAL_NERVE_FIBERS_HTML,
    "rows": [],
    "extra_search": "cranial nerve fibers phone 251 sse ssa sve sva gsa gvep gva gse table",
}


def _build_anatomy_list():
    out = list(_ANATOMY_DATA)
    for i, c in enumerate(out):
        if c["title"] == "Cranial nerves — rhyme & S/M/B":
            out.insert(i + 1, _fib)
            break
    else:
        out.append(_fib)
    return out


ANATOMY_MNEMONICS_SECTION = {
    "slug": "anatomy-mnemonics",
    "title": "Anatomy Mnemonics",
    "subtitle": "Systems-based anatomy recall cues (with Cranial Nerve Fibers grid + diagram).",
    "icon": "bx bx-body",
    "mnemonics": _build_anatomy_list(),
}
