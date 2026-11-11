# MCP use cases

A soft acceptance harness for the answers an LLM gives when it uses the yente
MCP server. Run each prompt verbatim in an MCP client, read the answer, and
check it against the criteria. The criteria judge what the answer conveys,
not how it's laid out or which tools produced it: the tool surface is
expected to change, and a bullet list or a single sentence is fine wherever
it carries the substance.

## What we'd like to inspire in MCP consumers

These are nudges, not pass/fail criteria.

- **Labels, not codes.** Show "Sanctioned entity" rather than `sanction`,
  "Russia" rather than `ru`, "Date of birth" rather than `birthDate`, and
  dataset and program titles rather than their keys. This covers topics,
  countries, datasets, programs and FtM property names.
- **Tables for structured facts.** Candidates, designations, relations and
  provenance read well as tables. Prose suits biographical narrative.
- **Citable evidence for every hit.** Give each hit an entity ID, source
  dataset and, where available, a source URL, in a form that survives being
  pasted into a spreadsheet row or an escalation packet.
- **Hits are evidence, not verdicts.** Present matches as input to a human
  reviewer's decision: lay out what agrees, what conflicts and what would
  settle it, but don't clear, reject or escalate a customer, or recommend
  doing so.

## How KYC agents use a screening MCP

Anthropic's [`kyc-screener`](https://github.com/anthropics/financial-services/tree/main/plugins/agent-plugins/kyc-screener)
agent template shows how a downstream agent is likely to use this server:

- **It's a generic slot.** The agent connects to an unnamed "screening" MCP
  by URL and expects no particular tool names or response shape. yente can
  fill that slot as long as the answers carry the substance.
- **It expects sanctions, PEP and adverse-media hits with match confidence,
  per named party.** We cover sanctions and PEPs directly. Adverse media is
  only covered indirectly, through crime- and regulatory-action topics.
- **The consumer is often a pipeline, not a chat.** Results go into a
  structured disposition and a spreadsheet for a compliance officer, so IDs
  and sources need to be machine-extractable.
- **It screens every named party.** One onboarding means several separate
  queries (company, directors, owners), and sometimes re-screening just a
  subset after the facts change.
- **It checks screening against self-declaration.** The rules compare an
  applicant's declared PEP status with what screening finds, so a
  contradiction between the two is itself a finding.
- **Our output is also untrusted input.** The template isolates applicant
  documents from the agent that holds the screening connector. But our
  results carry third-party text too (sanction reasons, notes, names), which
  that agent shouldn't follow as instructions either.

## Use cases

### 1. Screen a name with no hit

**Prompt:** Screen "John Smith-Preston-Warner" for sanctions.

**Expected query:** a matching query (not a search) on the name, restricted
to the `sanction`, `sanction.linked` and `sanction.control` topics.

**Acceptance criteria:**

- States that John Smith-Preston-Warner is not sanctioned.

### 2. Screen a sanctioned person (misspelled)

**Prompt:** Screen "Arkady Rothenberg" for sanctions.

**Expected query:** same as case 1. The misspelling of "Rotenberg" is
intentional.

**Acceptance criteria:**

- Identifies Arkady Romanovich Rotenberg as a match.
- Summarises biodata: name, date and place of birth,
  nationality/citizenship.
- Lists risk topics as labels.
- Puts the matcher's explanation into plain words, e.g. that the name
  matched despite the spelling difference. We're testing how well the
  consumer reads the `explanation` field, so we don't prescribe the wording.
- *Optional:* which countries designated him, under which sanctions programs.

### 3. Recognise a false positive

#### 3a. Fuzzy surname

**Prompt:** Screen "Arkady Rozenberg" for sanctions.

**Expected query:** same as case 1.

**Acceptance criteria:**

- Reports Arkady Romanovich Rotenberg as a possible match that needs review,
  not as a confirmed hit.
- Reads the explanation: the first name matches exactly, but the surname
  only matches fuzzily ("Rozenberg" vs "Rotenberg").
- Suggests what would settle it, such as date of birth or nationality.

> Observed 2026-11-04: scores 0.81 with `match: true`. Adding a birth date
> (e.g. 1985) or a nationality (e.g. Israel) drops the candidate entirely.

#### 3b. Initial matched to a patronymic

**Prompt:** Screen "A. Rotenberg", born 1985, for sanctions.

**Expected query:** same as case 1, including the birth date.

**Acceptance criteria:**

- Reports Pavel Arkadyevich Rotenberg as a weak match that needs review,
  not as a confirmed hit.
- Reads the explanation: the initial "A." matched his patronymic
  (Arkadyevich), not a given name, and the birth year conflicts (1985 vs
  2000).
- Notes that an initial plus a surname is too thin to identify anyone, and
  asks for a full name.

> Observed 2026-11-04: without the birth date it scores 0.87 with
> `match: true`. With 1985 it still scores 0.72 with `match: true`, despite
> the birth-year mismatch.

### 4. Trace the source of a sanction link

**Prompt:** "SHIRIN LTD" is sanction-linked. What is the risk source?

**Expected query:** resolves the company, then looks up its adjacent records
to find the connection to a sanctioned entity.

**Acceptance criteria:**

- Names the sanctioned entity that SHIRIN LTD is linked to.
- Shows the chain: the named entity, then the link (its type, e.g.
  ownership or directorship, and its metadata such as role, share or dates),
  then the sanctioned entity with its topics.

### 5. Profile a PEP, then assess reliability

**Prompt:** Abdul El Sayed is a PEP. What do we know about him?

**Acceptance criteria:**

- Covers positions held (role, country, dates where known), relatives and
  close associates, and basic biodata.

**Follow-up:** How reliable is this information?

- Shows record provenance: which sources assert which facts, and when they
  were first and last seen.

### 6. Rule out a PEP on birth date

#### 6a. 1979-04-06

**Prompt:** Is Abdul El Sayed born 1979-04-06 a PEP?

**Expected query:** a matching query with the name and birth date.

**Acceptance criteria:**

- Answers no.
- Attributes this to the birth-date mismatch and shows the matcher's
  explanation.

> Observed 2026-11-11: no results above the threshold, but Abdul El-Sayed
> (born 1984-10-31) comes back among the near misses at 0.60 with
> `match: false`, scored down by the birth-year and birth-day mismatch
> features. Two Egyptian senators sharing "El" and "Sayed" are near misses
> too.

#### 6b. 1984-01-05

**Prompt:** Is Abdul El Sayed born 1984-01-05 a PEP?

**Expected query:** same as 6a.

**Acceptance criteria:**

- Doesn't give a flat yes or no. Reports a likely match that needs review:
  the name and birth year agree, but the day and month don't.
- Shows the conflicting dates (1984-01-05 vs 1984-10-31) from the matcher's
  explanation.

> Observed 2026-11-04: Abdul El-Sayed scores 0.75 with `match: true`. Only
> the birth-day mismatch feature fires; the year agrees.

### 7. List sanctions programs and dates

**Prompt:** Show me what programs Rotenberg is sanctioned under.

**Expected query:** fetches the sanction records attached to the entity.

**Acceptance criteria:**

- Lists each program with the designating authority or country.
- Includes dates (listing, start, end) where present.

### 8. Large ownership fan-out

**Prompt:** Show me the subsidiaries of OAO Gazprom.

**Expected query:** looks up the company's ownership relations. This one is
expected to hit result limits.

**Acceptance criteria:**

- Says how many subsidiaries were shown and how many exist, or otherwise
  makes clear the list is truncated.
- Doesn't present a partial list as complete.

### 9. Ground answers in the data

**Prompt:** Who is Vladimir Putin?

**Acceptance criteria:**

- Draws on the record returned by the API: names, positions, topics,
  sanctions.
- Doesn't substitute the model's background knowledge for the data, or
  marks clearly where it does.

### 10. Screen a vessel by identifier

**Prompt:** Is the vessel with IMO 9179842 sanctioned?

**Expected query:** a matching query on a vessel using the IMO number.

**Acceptance criteria:**

- Identifies the vessel (Angara) as sanctioned.
- Says the match rests on the IMO number.
- Shows flag and owner/operator, with labels for countries.

### 11. Common name with thin data

**Prompt:** Screen Mohammed Ali, Pakistani national.

**Expected query:** a matching query with name and nationality, restricted
to the sanction topics.

**Acceptance criteria:**

- Doesn't overclaim. Many candidates score above the threshold on name
  alone, so the answer says no candidate is clearly the same person, or
  lists a few near-misses with why they fall short.
- Suggests what extra data (date of birth, ID numbers) would disambiguate.

### 12. Non-Latin script input

**Prompt:** Screen Аркадий Ротенберг.

**Acceptance criteria:**

- Finds the same record as case 2.
- Presents names sensibly for the user's script and language.

### 13. Data freshness and coverage

**Prompt:** How current is the US OFAC data you're checking against?

**Acceptance criteria:**

- Answers from dataset metadata returned by the API (publisher, last
  updated), not from model knowledge.

### 14. Onboarding: screen every named party

**Prompt:** Onboard Blue Harbour Trading GmbH (Germany). Director: Jane
Miller. Beneficial owner: Arkady Rotenberg, 30%.

**Expected query:** a separate, unrestricted matching query for each party
(the company, the director and the beneficial owner).

**Acceptance criteria:**

- Screens all three parties and reports a result for each.
- The owner is a sanctions hit; the company has no candidates; the director
  only has name-only candidates, so the answer asks for her birth date or
  nationality rather than clearing or flagging her.
- Doesn't approve, decline or escalate the onboarding, or recommend any of
  these.

> Observed 2026-11-11: the company (fictional) returns no candidates. Jane
> Miller returns name-only candidates, among them a UK diplomat (PEP) and
> US healthcare debarments.

### 15. Ongoing monitoring: what changed

**Prompt:** We last reviewed Boris Romanovich Rotenberg on 2026-10-01. Has
anything changed since?

**Expected query:** resolves the person, then checks dates on the record, its
statements and its sanction records against the review date.

**Acceptance criteria:**

- Answers from dates in the data, not from general knowledge.
- Separates material changes (a new or ended sanction) from cosmetic ones
  (a new name spelling).
- Reports the new EU travel-ban listing.
- Doesn't say "nothing changed" without having checked the sanction records
  as well as the person's own statements.

> Observed 2026-11-04: one new sanction record, an EU travel ban from the
> Council of the European Union, first seen 2026-10-13. One new statement:
> the name "Boris Rotenberg Sr." from Wikidata, first seen 2026-11-04.

### 16. Declared vs. screened PEP status

**Prompt:** Applicant Abdul El-Sayed, born 1984-10-31, declares he is not a
PEP. Check his declaration.

**Expected query:** a matching query with the name and birth date.

**Acceptance criteria:**

- Finds that screening contradicts the declaration.
- Says what the PEP status rests on, as far as the data shows, including
  any gaps.
- Flags the contradiction for review rather than deciding whether the
  applicant misstated his status.

> Observed 2026-11-11: Abdul El-Sayed matches at 1.0 on name and birth date,
> with the Politician and PEP topics. His one position is United States
> senator, starting 2027-01-03, from Wikidata only and first seen
> 2026-11-05, so the PEP status rests on a future, crowd-sourced occupancy.

### 17. Risk listing without a sanction

**Prompt:** Screen Filaret Galchev.

**Expected query:** a matching query with no topic restriction (a
sanctions-only screen would never return this person), then a profile of
the candidate.

**Acceptance criteria:**

- Says the person is not sanctioned.
- Reports the risk listing as a finding, not a sanction, with its source and
  program (the CAATSA Section 241 oligarch report).

> Observed 2026-11-11: Filaret Galchev (`Q4132909`) carries the Oligarch
> topic and one Risk record from `wd_oligarchs` (program `US-CAATSA-241`,
> authority OpenSanctions, no reason text), and no Sanction.
