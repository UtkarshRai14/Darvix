# Native-language voice bots: Philippines and Indonesia (Part 3)

Both bots run on the Part 1 engine with market-specific profiles, knowledge collections, ASR configuration and
voices. The design aim is localisation, not translation:
- each market's script, rules, objections and fallbacks are written for that market's sector and register;
- the knowledge base holds market-specific facts (peso premiums and grace periods; rupiah penalties and payment channels);
- a per-market glossary maps colloquial words to KB terms.

| | Philippines | Indonesia |
|---|---|---|
| Profile | `profiles/life_premium_ph.yaml` | `profiles/multifinance_id.yaml` |
| Sector / flow | Life insurance (bancassurance via Sinag Bank): **premium reminder** with lapse prevention, objection handling and bank-referral cross-sell | Multifinance (motorcycle financing): **installment reminder** / early collections with promise-to-pay |
| Languages | English, Filipino/Tagalog, Taglish; mirrors the customer | Formal and colloquial Bahasa Indonesia; English finance loanwords; Javanese/Sundanese words understood |
| KB | `life_ph` (English source docs, as PH insurers write them; queried in Taglish) | `finance_id` (Indonesian source docs) |
| ASR | Whisper `language=tl` + Taglish prompt (`premium, policy, beneficiary, rider, lapse, coverage, grace period, auto-debit, Sinag Bank…`) | Whisper `language=id` + prompt with `angsuran, cicilan, tenor, denda, DP, jatuh tempo, pembiayaan, virtual account, BPKB, nggih, mboten, sampun…` |
| TTS (native) | `fil-PH-BlessicaNeural` (agent), `fil-PH-AngeloNeural` (test customer) | `id-ID-GadisNeural` (agent), `id-ID-ArdiNeural` (test customer), `jv-ID-DimasNeural` (Javanese-accent test customer) |
| Greeting by local time | Magandang umaga / hapon / gabi po | Selamat pagi / siang / sore / malam |
| Politeness | "po/opo", "Sir Mark" | "Bapak/Ibu", "Pak Budi" |
| Identity | Policyholder confirmed + last 4 digits of the policy number. Account details are only put into the LLM context after code-side verification. | Same, with the last 4 digits of the contract number |
| Money and dates | ₱9,850 said the Filipino way ("nine thousand eight hundred fifty pesos"); English month names ("October 27") | Rp1.275.000 said "satu juta dua ratus tujuh puluh lima ribu rupiah"; "8 Oktober" |
| Deterministic rules | Grace end = due + 31 days; a promise after grace end is escalated; cancellation goes to a retention specialist; hardship goes to a financial advisor | Penalty = 0.2%/day of the installment, capped at 20% (computed in code); promise to pay ≤ 7 days, otherwise branch officer; restructuring/PHK goes to a branch officer; calling-hours flag (08:00-20:00 Mon-Sat local) |
| Compliance tone | Lapse explained as information, never as a threat | No threats, no shaming, motorcycle repossession never used as pressure |

## Localisation vs literal translation

Each example compares what a literal translation of an English script produces with what the bot is designed to
say. The localized lines come from the profiles, fallbacks and objection guides; reminder amounts and dates are
filled in from the account context at run time.

### Philippines

**1. The reminder itself**
- *Literal:* "Ang inyong bayad sa premium ay lampas na sa takdang petsa. Mangyaring magbayad kaagad upang maiwasan ang pagkawala ng bisa ng polisiya."
- *Localized:* "Paalala lang po, Sir Mark - yung quarterly premium ninyo na nine thousand eight hundred fifty pesos ay due na noong September 26. Covered pa rin po kayo hanggang October 27 dahil sa grace period, kaya walang penalty basta mabayaran bago noon."
- *Why:* textbook Filipino ("takdang petsa", "pagkawala ng bisa") sounds like a legal notice. Bank and insurance call centres speak Taglish and keep the English finance terms customers use (premium, due, covered, grace period). The line opens with reassurance, so lapse is information, not a threat.

**2. Objection: "Wala pa akong pera ngayon"**
- *Literal:* "Nauunawaan ko ang inyong sitwasyong pinansyal. Nais ba ninyo ng impormasyon tungkol sa Awtomatikong Pautang sa Premium?"
- *Localized (objection guide):* "Naiintindihan ko po. Covered pa rin po kayo hanggang sa end ng grace period, at walang penalty basta mabayaran within that period. Kailan po kaya kayo makakapag-settle?"
- *Why:* empathy first in everyday register. Product names such as "Automatic Premium Loan" are never translated. Asking "kailan po kaya" is a softer way to get a commitment date.

**3. Fallback when information is unavailable**
- *Literal:* "Hindi ko masasagot iyan. Ililipat kita sa isang taong ahente."
- *Localized:* "Pasensya na po, wala po akong verified na impormasyon tungkol diyan. Pwede ko po kayong i-refer sa isang Liwanag financial advisor para masagot po iyan nang tama."
- *Why:* the literal version uses informal "kita" and the unnatural "taong ahente". The fallback stays in Taglish with "po", uses "i-refer" (bank vocabulary) and the real job title. It does not switch to English.

**4. Mirroring:** if the customer speaks straight English, the style rules have the bot answer in English with
"po". If they speak deep Filipino, it uses more Filipino. A code check regenerates any reply with no Filipino at all.

### Indonesia

**1. The reminder**
- *Literal:* "Cicilan Anda sudah terlambat. Penalti akan dikenakan."
- *Localized:* "Mohon maaf mengganggu waktunya, Pak Budi. Kami mau mengingatkan angsuran ke-14 sebesar satu juta dua ratus tujuh puluh lima ribu rupiah yang jatuh tempo tanggal 28 September kemarin. Saat ini sudah berjalan denda tujuh ribu enam ratus lima puluh rupiah."
- *Why:* "Mohon maaf mengganggu" is the standard customer-service opener. "Pak" replaces the stiff spoken "Anda". "Angsuran", "jatuh tempo" and "denda" are the industry terms (not "penalti"). The penalty is computed in code (3 days × 0.2% × Rp1.275.000 = Rp7.650), so it is never LLM arithmetic.

**2. Colloquial / Javanese-flavoured customer**
- *Customer:* "Nggih, Mbak, tapi kulo dereng gajian, dendane kok larang men."
- *Bot (style rules):* "Baik, Pak, saya mengerti. Dendanya memang dihitung 0,2% per hari dari angsuran, jadi makin cepat dibayar makin kecil. Kira-kira Bapak bisa bayar tanggal berapa?"
- *Why:* the bot understands the Javanese words (kulo = saya, dereng = belum, larang = mahal) but answers in simple, polite Indonesian. Imitating Javanese risks using the wrong *krama* level, and the national language is the safe register. The ASR prompt includes common Javanese particles so they are transcribed rather than dropped.

**3. Fallback**
- *Literal:* "Saya tidak tahu. Silakan hubungi layanan pelanggan."
- *Localized:* "Mohon maaf, saya belum punya informasi yang pasti untuk hal itu. Saya bisa minta petugas kantor cabang menghubungi Bapak untuk menjelaskannya."
- *Why:* "belum punya informasi yang pasti" saves face compared with "tidak tahu", and the bot offers a concrete next step.

**4. Loanwords:** "DP", "tenor" and "virtual account" are kept as Indonesians say them; "virtual account" is never
turned into "rekening virtual". A code check regenerates replies that drift into English sentences.

## Terminology

| Term | PH usage in the bot | ID term | ID usage in the bot |
|---|---|---|---|
| premium | kept in English ("yung premium ninyo") | angsuran / cicilan | "angsuran"; mirrors "cicilan" if the customer uses it |
| policy | "policy" | pembiayaan | "pembiayaan motor" |
| beneficiary | "beneficiary"; change only by form at a branch | tenor | "tenor 24 bulan", "sisa tenor" |
| rider | "rider" (Accidental Death Benefit) | denda | 0.2%/day, capped at 20% |
| lapse | "ma-lapse" (Taglish verb form) | DP | "DP (uang muka)" |
| coverage | "covered pa rin po kayo" | jatuh tempo | "jatuh tempo tanggal 28 September" |
| bank referral | "i-refer sa Liwanag financial advisor sa Sinag Bank" | janji bayar | promise to pay, ≤ 7 days |

The retrieval glossaries (`query_expansions` in each `sources.json`) cover colloquial forms:
- PH: `pera`, `pambayad`, `hulog`, `ipa-cancel`, `na-lapse`.
- ID: `cicilan`, `telat`, `nunggak`, `PHK`, `nggak`, `mboten`, `gajian`, `over kredit`.

## Language-specific ASR: configuration and how it is measured

- **Provider/model:** Groq Whisper `whisper-large-v3-turbo` for the bots (the live-insights module also uses `whisper-large-v3`).
- **Configured per market:**
  - **PH:** `language=tl`. Forcing Tagalog stops Whisper translating Taglish into English. English words inside Taglish are expected to be kept; the eval checks this.
  - **ID:** `language=id`.
  - A **domain prompt** in each market's own register primes product terms and particles.
- **Measured by** `python -m app.asr_eval` → `reports/asr_report.md`:
  - 10 PH utterances: Filipino, Taglish, finance terms, colloquial, escalation, English-heavy.
  - 10 ID utterances: formal, numbers, colloquial, loanwords, Javanese words.
  - Each runs in the **configured** mode and a **baseline** (auto-detect, no prompt).
  - Reports WER, finance-term recall (were terms transcribed rather than dropped or translated), detected language, latency and per-utterance errors.
- **Indonesian regional accent:** the same 10 Indonesian sentences are also spoken by `jv-ID-DimasNeural` (Javanese) and `su-ID-JajangNeural` (Sundanese) and compared with the standard `id-ID` voice.

  This is a **proxy**: neural voices of a regional language reading Indonesian. It is not the same as real speakers from Surabaya or Bandung. To test real accents, drop WAVs at `data/asr_eval/human/multifinance_id/<voice_key>/<id>.wav` and re-run; human recordings automatically replace synthetic ones.
- **In-call ASR quality:** every simulated call also logs what the test customer said next to what Whisper heard, with WER per utterance (Call Log → test scenario).

No ASR numbers are quoted here because they must come from running the evaluation with a Groq key.

## Test calls

`tests/call_personas.yaml` contains two calls per market:

| Persona | Coverage |
|---|---|
| `ph_cooperative_taglish` | cooperative customer, mixed English/finance terms, Taglish code-switching, rider question, bank referral |
| `ph_objection_escalation` | sector objection (no money / wants to cancel / lapse question), colloquial Filipino, human escalation |
| `id_cooperative_formal` | cooperative customer, formal register, loanwords (DP, tenor, virtual account), penalty question |
| `id_javanese_colloquial` | **Javanese-accent voice**, colloquial speech with Javanese particles, penalty objection, restructuring request, human escalation |

Run `python -m app.agent.simulate ph_cooperative_taglish ph_objection_escalation id_cooperative_formal id_javanese_colloquial`.

## TTS compromises

- The `fil-PH` voices read the English words in Taglish with Filipino phonology, which is acceptable and close to
  how Filipinos speak, but long English phrases can sound flat. No Taglish-specific voice exists in the free tier.
- Only two voices per market are available (`fil-PH`: Blessica/Angelo, `id-ID`: Gadis/Ardi). There is no
  SSML-level control in edge-tts, so pacing relies on short sentences and amounts written in words.
- Javanese/Sundanese voices are used only to simulate accented customers, never for the agent.

## Known native-speaker and compliance gaps

- The scripts, guides and fallbacks were written without native-speaker review. Particles, register (especially
  Javanese *krama* vs *ngoko*) and regional expressions need review by native Filipino and Indonesian speakers.
- The Javanese/Sundanese accent results are synthetic proxies.
- Philippines: confirm grace-period, lapse and reinstatement wording against the actual policy contract and
  Insurance Commission requirements. Personal-data handling must follow the Data Privacy Act of 2012 (RA 10173),
  including consent for recording.
- Indonesia: collection conduct must follow OJK consumer-protection rules (POJK 22/2023 on consumer protection in
  the financial services sector), including contact hours and no intimidation or third-party disclosure.
  Personal data must follow UU PDP (Law 27/2022). Here the bot only *flags* calls outside 08:00-20:00 Mon-Sat; it does not block them.
- Penalty, promise-to-pay and restructuring rules are fictional company policy and must be replaced with the lender's contract terms.
