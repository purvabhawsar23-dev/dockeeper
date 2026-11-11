# DockKeeper: Results

## Evaluation (35 questions, 4 documents)

I tested the app with 35 questions on three small documents: an AquaPure 3000 water
purifier manual (2 pages), a hostel rules notice (1 page) and photosynthesis class notes
(1 page). Six of the questions have no answer in any document, so the correct behaviour
is a refusal. The questions and the scoring script were drafted with AI assistance. I ran
the evaluation, checked each failure against the source documents, and made the fixes below.

Each question is scored on three checks: the answer contains the expected key fact, a
cited source is the expected file and page, and unanswerable questions are refused with no
sources.

| Run | Score | Notes |
|---|---|---|
| Baseline | 29 of 35 | Two of the six failures were test keys that were too strict |
| After changes | 34 of 35 | See changes below |

Final result by category: manual 15 of 15, hostel 10 of 11, notes 4 of 4, refusal 5 of 5.

### Changes to the app
- Added grounding rules to the answer prompt: use only the sources' own terms, say when a
  case is not named, never add facts. This fixed an answer that treated "double room" as
  "shared room".
- Lowered the relevance threshold MIN_SIMILARITY from 0.58 to 0.55. A question about
  mounting screws scored 0.5798 and was wrongly refused.

### Changes to the test (stated openly)
- Q7 (RO membrane cost): accepts page 1 or 2, since the figure sits at a chunk boundary.
- Q25 (cooking in a room): expected key changed from "not" to "prohibited", the word the
  document uses.
- Q33 (Tuesday gates): moved from a refusal case to an answer case. The app now says the
  sources give no Tuesday-specific time and quotes the general weekday rule.

### Known miss
"What is the emergency number?" is refused although the number is in the hostel rules.
Its best vector similarity was about 0.53, below the 0.55 threshold, and keyword search
does not match the word "emergency". Lowering the threshold further would risk letting in
unrelated questions: "What is the capital of France?" scores about 0.51.

### Limits of this evaluation
- 35 questions on 4 short documents is a small test. It shows the system works on this
  set, not that it works in general.
- Some test changes were made after seeing the first results, so the final score is not a
  blind test.

## Stress test

- A generated 120-page text PDF (one marker code per page) reached "ready".
- Database check: 120 pages, 120 chunks, 0 missing vectors.
- Marker questions for pages 3, 97 and 118 returned the right code and cited the right page.
- Limit: each page was short, so this shows the pipeline runs the full length, not that the
  chunker copes with very dense pages. A 500-page dense book was not tested.