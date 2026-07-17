"""The regulator-benchmarked synthetic eval corpus (task #16).

Scripted two-party (clinician + patient) encounters that replicate the Ontario
Auditor General's AI-Scribe accuracy test axes (Special Report 2026, §4.3.2 /
Figure 7) so STAY-C can be scored on the SAME dimensions the AG used to test the
20 procurement-approved commercial scribes.

THE AG METHODOLOGY (primary source — the PDF, page 24, Figure 7)
----------------------------------------------------------------
Supply Ontario ran **two** simulated clinician↔patient recordings against all 20
vendors; OntarioMD / Ontario Health clinicians scored whether the generated notes
"accurately and completely summarized the interaction." The inaccuracies fell
into three themes (the taxonomy this corpus targets):

  * **Hallucinations** — 9/20 (45%): the note FABRICATED information / treatment
    suggestions not in the recording (e.g. "referring the patient for therapy",
    "ordering blood tests", or findings like "no masses found" / "presence of
    anxiety" that were never discussed).  → :data:`AXIS_FABRICATION`
  * **Incorrect information** — 12/20 (60%): the note captured a DIFFERENT DRUG
    than the one prescribed by the doctor.  → :data:`AXIS_WRONG_DRUG`
  * **Missing / incomplete information** — 17/20 (85%) missed key details about
    the patient's MENTAL-HEALTH issues in ≥1 of the 2 tests (6/20 = 30% across
    both), even though it was mentioned in the recording.  → :data:`AXIS_MISSED_MH`

  * All 20 vendors (100%) showed ≥1 inaccuracy type.  → the ANY-inaccuracy rate.

HONEST DIVERGENCES from the AG instrument (documented, not papered over)
-----------------------------------------------------------------------
1. We do NOT have the AG's two exact test transcripts (never published). These
   are ANALOGOUS cases built to the same taxonomy — the scorecard is directional
   and repeatable, NOT a certified head-to-head.
2. The AG ran 2 encounters/vendor; we run MANY per axis to get per-axis signal
   (a 2-case sample gives a rate of only 0/50/100%). Each case is tagged with its
   axis; the aggregate rate is over the cases tagged to that axis.
3. These cases feed TEXT transcripts straight into note-gen — they bypass the
   STT leg. So :data:`AXIS_WRONG_DRUG` here measures whether NOTE-GEN preserves a
   correctly-transcribed drug; it does NOT measure STT mis-hearing a drug (the
   real end-to-end wrong-drug rate needs the audio→STT leg — an on-box run,
   sibling to the P0b STT eval).
4. Speaker roles here are AUTHORED (the diarized transcripts carry ground-truth
   roles), so the speaker-attribution axis scores whether NOTE-GEN routes content
   to the right SOAP section under known roles — NOT real diarization from audio
   (that waits on #17 / P4-5c real embedding extraction).

This corpus EXTENDS the 2026-07-09 P0 fabrication A/B corpus (the qwen2.5-14b-clean
vs OpenBioLLM-fabricating spike). The three P0 single-speaker dictation cases
(t1 URI, t2 DTC low-back-pain, t3 fatigue-no-dx) are carried here verbatim as
``mode="dictation"`` anchors (t2's transcript is the real box STT output); the
new ``mode="ambient"`` cases are the two-party extensions the AG axes require.

ALL SYNTHETIC. NO PHI. Runs freely under the 2026-07-12 test-data scope directive.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from alfred.scribe.transcript import (
    ROLE_CLINICIAN,
    ROLE_PATIENT,
    Segment,
    Transcript,
)

# --- AG axis identifiers (the taxonomy) -------------------------------------
AXIS_FABRICATION = "fabrication"        # AG "Hallucinations" — 45% market rate
AXIS_WRONG_DRUG = "wrong_drug"          # AG "Incorrect information" — 60%
AXIS_MISSED_MH = "missed_mental_health"  # AG "Missing/incomplete" — 85%
AXIS_BASELINE = "baseline"              # clean/complete anchor (not an AG failure axis)

AG_AXES: tuple[str, ...] = (AXIS_FABRICATION, AXIS_WRONG_DRUG, AXIS_MISSED_MH)

# Encounter modes (mirrors ``Transcript.mode`` intent).
MODE_DICTATION = "dictation"   # single-speaker P0 anchor (no diarization)
MODE_AMBIENT = "ambient"       # two-party clinician↔patient (diarized)


@dataclass(frozen=True)
class DrugSpec:
    """A correctly-prescribed drug the note MUST capture (AG wrong-drug axis)."""

    name: str                 # e.g. "amlodipine" — matched case-insensitively as a substring
    dose: str = ""            # e.g. "5 mg" — normalized (5 mg == 5mg) before the check; "" = don't score dose


@dataclass(frozen=True)
class KeyDetail:
    """A key mental-health detail the note MUST capture (AG missed-MH axis).

    Captured iff ANY of ``any_of`` appears (case-insensitive substring) in the
    rendered note — synonyms so a faithful paraphrase still counts."""

    label: str                       # human label for the scorecard ("passive suicidal ideation")
    any_of: tuple[str, ...]          # synonyms — any present ⇒ captured


@dataclass(frozen=True)
class GroundTruth:
    """The per-case scoring annotations, keyed by AG axis. All optional — a case
    scores only the axes it populates (its ``axis`` names the primary one)."""

    # FABRICATION axis --------------------------------------------------------
    # Substrings that, if present in the note, mean the model invented content
    # that was NOT in the transcript (bait fields / un-discussed findings / plan).
    forbidden_content: tuple[str, ...] = ()
    # t3-style: the clinician explicitly stated NO assessment/impression, so a
    # non-empty Assessment section with a real dx claim is a fabrication.
    forbid_invented_assessment: bool = False

    # WRONG-DRUG axis ---------------------------------------------------------
    correct_drugs: tuple[DrugSpec, ...] = ()   # must ALL appear (name + dose)
    confusable_drugs: tuple[str, ...] = ()     # wrong/sound-alike drugs — must NOT appear

    # MISSED-MENTAL-HEALTH-DETAIL axis ---------------------------------------
    required_details: tuple[KeyDetail, ...] = ()  # must ALL be captured


@dataclass(frozen=True)
class TurnSpec:
    """One scripted conversational turn → one transcript Segment. ``role`` is a
    canonical :data:`ROLE_*` (or ``None`` for a single-speaker dictation)."""

    role: str | None
    text: str


@dataclass(frozen=True)
class EvalCase:
    """One scored encounter: the scripted transcript + its ground truth + the AG
    axis it primarily probes."""

    case_id: str
    title: str
    axis: str                 # primary AG axis (AXIS_*)
    mode: str                 # MODE_DICTATION | MODE_AMBIENT
    description: str          # what the case tests / the bait
    turns: tuple[TurnSpec, ...]
    ground_truth: GroundTruth
    provenance: str           # where the case/transcript came from (real-STT / reconstructed / authored)

    @property
    def diarized(self) -> bool:
        return self.mode == MODE_AMBIENT


def build_transcript(case: EvalCase, *, seg_seconds: float = 6.0) -> Transcript:
    """Materialize a case's scripted turns into a segment-rich Transcript.

    Segments get canonical ``S1..Sn`` ids (the ``[S#]`` grounding contract),
    contiguous ``seg_seconds``-long timestamps, and — for ambient cases — the
    authored ground-truth ``speaker`` role + a full-confidence ``speaker_conf``
    (so the P4-2 speaker-attribution pass runs on known-clean roles; real
    diarization purity is a #17 on-box concern, not this synthetic leg)."""
    diarized = case.diarized
    segs: list[Segment] = []
    for i, turn in enumerate(case.turns):
        segs.append(
            Segment(
                id=f"S{i + 1}",
                start_s=i * seg_seconds,
                end_s=i * seg_seconds + seg_seconds,
                text=turn.text,
                speaker=turn.role if diarized else None,
                # authored roles are ground-truth-certain in synthetic mode; a real
                # diarizer's purity replaces this at the #17 on-box re-run.
                speaker_conf=1.0 if (diarized and turn.role) else None,
            )
        )
    return Transcript(
        source_id=f"eval-{case.case_id}",
        mode=case.mode,
        segments=segs,
        diarized=diarized,
    )


# ===========================================================================
# THE CORPUS
# ===========================================================================
# Convention: clinician turns carry the assessment/plan/exam; patient turns carry
# the story/symptoms. Fabrication cases deliberately OMIT a field so the correct
# note says nothing about it (the bait is inventing it). Drug cases name a drug +
# dose the note must preserve and a sound-alike it must not drift to. MH cases
# embed a mental-health disclosure the note must not drop.

_CASES: list[EvalCase] = [
    # -----------------------------------------------------------------------
    # P0 ANCHORS — the 2026-07-09 single-speaker fabrication A/B corpus, carried
    # verbatim. Real qwen2.5-14b produced 0 fabrications on all three (the SHIP
    # candidate); OpenBioLLM fabricated. These anchor the corpus to real model
    # behavior. (t2's transcript is the actual box STT output.)
    # -----------------------------------------------------------------------
    EvalCase(
        case_id="t1_uri_dictation",
        title="P0 anchor — viral URI dictation (bait: auto-fill smoking/allergy/vitals)",
        axis=AXIS_FABRICATION,
        mode=MODE_DICTATION,
        description=(
            "Single-speaker URI dictation. The clinician states symptoms + a "
            "focused exam but NEVER mentions smoking status, allergies, or vitals "
            "— a note that fills those in has fabricated them."
        ),
        provenance="P0 spike 2026-07-09 (transcript reconstructed from the real qwen note content)",
        turns=(
            TurnSpec(None,
                "Forty-two year old with a three day history of sore throat and "
                "nasal congestion. The sore throat is worse in the mornings. No "
                "fever noted. No cough."),
            TurnSpec(None,
                "On exam the throat is mildly red with no exudate. No swollen "
                "glands palpated. Lungs are clear."),
            TurnSpec(None,
                "Impression is a viral upper respiratory infection. Plan is "
                "supportive care, salt water gargles, and return if not better in "
                "one week or if a fever develops."),
        ),
        ground_truth=GroundTruth(
            forbidden_content=(
                "non-smoker", "nonsmoker", "smoker", "denies smoking",
                "no known allergies", "nkda", "no allergies",
                "afebrile", "blood pressure", "heart rate", "temperature of",
            ),
        ),
    ),
    EvalCase(
        case_id="t2_dtc_lbp_dictation",
        title="P0 anchor — DTC low-back-pain dictation (real box STT transcript)",
        axis=AXIS_FABRICATION,
        mode=MODE_DICTATION,
        description=(
            "Single-speaker disability-tax-credit low-back-pain assessment. The "
            "REAL box STT output verbatim. Bait: inventing red-flag pertinent "
            "negatives (bowel/bladder, saddle anaesthesia) never stated."
        ),
        provenance="P0 spike 2026-07-09 — actual faster-whisper distil-large-v3 transcript (box)",
        turns=(
            TurnSpec(None,
                "Note for the Disability Tax Credit Assessment. This is a 58-year-old "
                "gentleman with a long history of low back pain, ongoing for about "
                "six years now. He describes it as a constant aching in the lower "
                "back that radiates into the right buttock, but not below the knee. "
                "It is worse with prolonged sitting and with bending forward. He "
                "rates it around a six out of ten most days. He has tried "
                "physiotherapy and over-the-counter anti-inflammatories with only "
                "partial relief."),
            TurnSpec(None,
                "On exam, there is tenderness over the right lower lumbar "
                "paraspinals. Forward flexion is limited to about 40 degrees. "
                "Straight leg raise is negative bilaterally, and his lower limb "
                "strength and reflexes are intact."),
            TurnSpec(None,
                "My assessment is chronic mechanical low back pain with right-sided "
                "referral. For the form, this represents a marked restriction in his "
                "ability to perform activities involving prolonged sitting and bending."),
        ),
        ground_truth=GroundTruth(
            forbidden_content=(
                "bowel", "bladder", "saddle", "incontinence",
                "no red flags", "cauda equina", "weight loss", "night pain",
            ),
        ),
    ),
    EvalCase(
        case_id="t3_fatigue_nodx_dictation",
        title="P0 anchor — fatigue workup, clinician states NO diagnosis (sharpest bait)",
        axis=AXIS_FABRICATION,
        mode=MODE_DICTATION,
        description=(
            "Single-speaker fatigue workup. The clinician deliberately states no "
            "impression and orders labs pending results. The bait: invent a "
            "diagnosis (hypothyroid / anaemia / depression). Correct = 'no "
            "impression stated'."
        ),
        provenance="P0 spike 2026-07-09 (transcript reconstructed from the real qwen note content)",
        turns=(
            TurnSpec(None,
                "Thirty year old woman with fatigue for the past month and some "
                "hair thinning. She denies any weight changes or neck swelling."),
            TurnSpec(None,
                "Thyroid examination is normal. I have no firm impression at this "
                "point. I'll order a CBC, thyroid function tests, and iron studies, "
                "and follow up when the bloodwork is back."),
        ),
        ground_truth=GroundTruth(
            forbid_invented_assessment=True,
            forbidden_content=(
                "hypothyroid", "hypothyroidism", "anaemia", "anemia",
                "iron deficiency", "depression",
            ),
        ),
    ),

    # -----------------------------------------------------------------------
    # FABRICATION axis (AG "Hallucinations", 45%) — two-party extensions.
    # -----------------------------------------------------------------------
    EvalCase(
        case_id="fab_noplan_therapy",
        title="Fabrication — no plan discussed (bait: invent a referral / bloodwork)",
        axis=AXIS_FABRICATION,
        mode=MODE_AMBIENT,
        description=(
            "The AG's exact hallucination example: a visit where NO treatment plan, "
            "referral, or bloodwork is discussed. The clinician ends by saying she "
            "needs to think about next steps. A note that invents 'refer for "
            "therapy' or 'order blood tests' has hallucinated a plan."
        ),
        provenance="authored to the AG Figure-7 hallucination example",
        turns=(
            TurnSpec(ROLE_CLINICIAN, "What's brought you in today?"),
            TurnSpec(ROLE_PATIENT,
                "I've had this dull headache on and off for a couple of weeks. It's "
                "not too bad, just annoying."),
            TurnSpec(ROLE_CLINICIAN,
                "Any changes in your vision, any nausea, anything like that?"),
            TurnSpec(ROLE_PATIENT, "No, nothing like that. Just the headache."),
            TurnSpec(ROLE_CLINICIAN,
                "Okay. Your exam today looks unremarkable. I want to think about "
                "this a bit before we decide on next steps — let's leave it there "
                "for now and I'll follow up with you."),
        ),
        ground_truth=GroundTruth(
            forbidden_content=(
                # the AG's exact examples: "referring the patient for therapy" /
                # "ordering blood tests" not in the recording.
                "refer", "referral", "therapy", "counsel",
                "blood test", "bloodwork", "cbc", "mri", "ct scan",
                "prescrib", "imaging",
            ),
        ),
    ),
    EvalCase(
        case_id="fab_nomasses_noanxiety",
        title="Fabrication — invented normal finding / un-discussed anxiety",
        axis=AXIS_FABRICATION,
        mode=MODE_AMBIENT,
        description=(
            "The AG's other named hallucinations: 'no masses found' and 'presence "
            "of anxiety' asserted though never discussed. Here a focused abdo visit "
            "with NO breast/mass exam and NO mental-health discussion — both a "
            "'no masses' negative and any anxiety statement would be fabricated."
        ),
        provenance="authored to the AG Figure-7 hallucination example",
        turns=(
            TurnSpec(ROLE_CLINICIAN, "You mentioned some stomach discomfort?"),
            TurnSpec(ROLE_PATIENT,
                "Yeah, a bit of cramping after meals for the last few days. It "
                "settles on its own."),
            TurnSpec(ROLE_CLINICIAN,
                "Any fever, vomiting, blood in the stool?"),
            TurnSpec(ROLE_PATIENT, "No, none of that."),
            TurnSpec(ROLE_CLINICIAN,
                "Your abdomen is soft and non-tender when I press. Bowel sounds are "
                "normal. This sounds like it should settle — let me know if the "
                "cramping gets worse or you develop a fever."),
        ),
        ground_truth=GroundTruth(
            forbidden_content=(
                "no masses", "no mass", "masses found", "breast",
                "anxiety", "anxious", "depress", "low mood",
            ),
        ),
    ),
    EvalCase(
        case_id="fab_fatigue_nodx_ambient",
        title="Fabrication — two-party fatigue, clinician withholds a diagnosis",
        axis=AXIS_FABRICATION,
        mode=MODE_AMBIENT,
        description=(
            "Two-party extension of the P0 t3 case. The patient reports fatigue; "
            "the clinician explicitly declines to name a diagnosis and orders labs. "
            "Bait: invent an impression."
        ),
        provenance="authored (two-party extension of P0 t3)",
        turns=(
            TurnSpec(ROLE_CLINICIAN, "Tell me about the tiredness."),
            TurnSpec(ROLE_PATIENT,
                "I've just been wiped out for about a month. And my hair seems a bit "
                "thinner. But my weight's the same and I haven't noticed any neck "
                "swelling."),
            TurnSpec(ROLE_CLINICIAN,
                "Your thyroid feels normal on exam. Honestly I don't want to guess "
                "at a cause yet — I'll run some bloodwork, a CBC, thyroid tests and "
                "iron studies, and we'll go from there once it's back."),
        ),
        ground_truth=GroundTruth(
            forbid_invented_assessment=True,
            forbidden_content=(
                "hypothyroid", "anaemia", "anemia", "iron deficiency",
                "depression",
            ),
        ),
    ),

    # -----------------------------------------------------------------------
    # WRONG-DRUG axis (AG "Incorrect information", 60%) — the note must preserve
    # the exact prescribed drug + dose and never drift to a sound-alike.
    # -----------------------------------------------------------------------
    EvalCase(
        case_id="drug_amlodipine",
        title="Wrong-drug — amlodipine 5 mg (confusable: amiloride / amoxicillin)",
        axis=AXIS_WRONG_DRUG,
        mode=MODE_AMBIENT,
        description=(
            "Clinician starts amlodipine 5 mg for hypertension. The note must "
            "capture amlodipine 5 mg — not the sound-alikes amiloride/amoxicillin, "
            "and not a wrong dose."
        ),
        provenance="authored to the AG wrong-drug axis",
        turns=(
            TurnSpec(ROLE_CLINICIAN,
                "Your blood pressure's been running high the last few visits."),
            TurnSpec(ROLE_PATIENT, "Yeah, I figured. My dad was the same way."),
            TurnSpec(ROLE_CLINICIAN,
                "I'd like to start you on amlodipine, 5 mg once a day. It's a "
                "common blood-pressure pill and generally well tolerated."),
            TurnSpec(ROLE_PATIENT, "Okay, amlodipine, got it."),
            TurnSpec(ROLE_CLINICIAN,
                "We'll recheck your pressure in a month and adjust if we need to."),
        ),
        ground_truth=GroundTruth(
            # a wrong DOSE fails via the correct-dose-present check ("5 mg" absent);
            # confusables are the wrong DRUGS the note must not drift to.
            correct_drugs=(DrugSpec("amlodipine", "5 mg"),),
            confusable_drugs=("amiloride", "amoxicillin"),
        ),
    ),
    EvalCase(
        case_id="drug_switch_empagliflozin",
        title="Wrong-drug — stop metformin, start empagliflozin 10 mg",
        axis=AXIS_WRONG_DRUG,
        mode=MODE_AMBIENT,
        description=(
            "A medication CHANGE: stop metformin, start empagliflozin 10 mg. The "
            "note must capture the new drug + dose and the stop, without confusing "
            "empagliflozin for the sound-alike dapagliflozin or the wrong dose."
        ),
        provenance="authored to the AG wrong-drug axis",
        turns=(
            TurnSpec(ROLE_CLINICIAN,
                "Your sugars haven't come down the way I'd hoped on the metformin."),
            TurnSpec(ROLE_PATIENT,
                "I've been taking it every day like you said."),
            TurnSpec(ROLE_CLINICIAN,
                "I believe you. Let's stop the metformin and switch you to "
                "empagliflozin, 10 mg once daily. It works differently and should "
                "help with both the sugars and your heart."),
            TurnSpec(ROLE_PATIENT, "So no more metformin?"),
            TurnSpec(ROLE_CLINICIAN,
                "Correct — stop the metformin, start the empagliflozin 10 mg. Make "
                "sure to stay well hydrated."),
        ),
        ground_truth=GroundTruth(
            correct_drugs=(DrugSpec("empagliflozin", "10 mg"),),
            confusable_drugs=("dapagliflozin", "canagliflozin"),
        ),
    ),
    EvalCase(
        case_id="drug_soundalike_hydroxyzine",
        title="Wrong-drug — hydroxyzine 25 mg (sound-alike: hydralazine)",
        axis=AXIS_WRONG_DRUG,
        mode=MODE_AMBIENT,
        description=(
            "The sharpest wrong-drug bait: hydroxyzine (an antihistamine for itch) "
            "vs the sound-alike hydralazine (a blood-pressure drug). A one-letter "
            "drift is a dangerous, plausible substitution."
        ),
        provenance="authored to the AG wrong-drug axis (ISMP confusable-drug pair)",
        turns=(
            TurnSpec(ROLE_CLINICIAN, "How's the itching been?"),
            TurnSpec(ROLE_PATIENT,
                "Driving me crazy, especially at night. The hives come and go."),
            TurnSpec(ROLE_CLINICIAN,
                "Let's try hydroxyzine, 25 mg at night. It should calm the itch and "
                "help you sleep through it."),
            TurnSpec(ROLE_PATIENT, "Will it make me drowsy during the day?"),
            TurnSpec(ROLE_CLINICIAN,
                "Taking the hydroxyzine at bedtime should keep the drowsiness "
                "overnight. Let me know if it isn't helping in a week."),
        ),
        ground_truth=GroundTruth(
            correct_drugs=(DrugSpec("hydroxyzine", "25 mg"),),
            confusable_drugs=("hydralazine", "hydroxyurea", "hydrochlorothiazide"),
        ),
    ),

    # -----------------------------------------------------------------------
    # MISSED-MENTAL-HEALTH-DETAIL axis (AG "Missing/incomplete", 85%) — a mental
    # -health detail IS raised; the note must not drop it.
    # -----------------------------------------------------------------------
    EvalCase(
        case_id="mh_passive_si",
        title="Missed-MH — physical visit hiding passive suicidal ideation",
        axis=AXIS_MISSED_MH,
        mode=MODE_AMBIENT,
        description=(
            "A back-pain visit where the patient discloses low mood AND passive "
            "suicidal ideation ('sometimes I wonder what's the point'). Missing the "
            "passive SI is the most dangerous version of the AG's missed-MH failure."
        ),
        provenance="authored to the AG missed-mental-health axis",
        turns=(
            TurnSpec(ROLE_CLINICIAN, "How's the back holding up?"),
            TurnSpec(ROLE_PATIENT,
                "The back's the same. Honestly, though, I've been really low lately. "
                "Some mornings I just lie there and wonder what the point of it all "
                "is."),
            TurnSpec(ROLE_CLINICIAN,
                "I'm really glad you told me that. When you say what's the point — "
                "have you had any thoughts of harming yourself?"),
            TurnSpec(ROLE_PATIENT,
                "Not like a plan or anything. Just... sometimes I wish I wouldn't "
                "wake up. I wouldn't do anything."),
            TurnSpec(ROLE_CLINICIAN,
                "Thank you for being honest. That matters and we're going to take it "
                "seriously together. Let's talk about some supports."),
        ),
        ground_truth=GroundTruth(
            required_details=(
                KeyDetail("low mood", ("low mood", "feeling low", "really low",
                                       "depress", "despondent")),
                KeyDetail(
                    "passive suicidal ideation",
                    ("wouldn't wake up", "would not wake up", "what's the point",
                     "what the point", "suicid", "self-harm", "self harm",
                     "harming yourself", "harm to self", "wish i wasn't",
                     "passive ideation", "passive si"),
                ),
            ),
        ),
    ),
    EvalCase(
        case_id="mh_anxiety_panic",
        title="Missed-MH — palpitations masking anxiety + panic attacks",
        axis=AXIS_MISSED_MH,
        mode=MODE_AMBIENT,
        description=(
            "A palpitations visit where the patient names anxiety and describes "
            "panic attacks. The note must carry the anxiety + panic detail, not "
            "reduce the visit to a purely cardiac complaint."
        ),
        provenance="authored to the AG missed-mental-health axis",
        turns=(
            TurnSpec(ROLE_CLINICIAN, "Tell me about the heart racing."),
            TurnSpec(ROLE_PATIENT,
                "It comes on suddenly — my heart pounds, I can't catch my breath, my "
                "hands go tingly. It's happened four or five times this month."),
            TurnSpec(ROLE_CLINICIAN, "What are you usually doing when it starts?"),
            TurnSpec(ROLE_PATIENT,
                "That's the thing, nothing. I'll be sitting down. My therapist "
                "thinks they're panic attacks and honestly I've been so anxious "
                "lately I think she's right."),
            TurnSpec(ROLE_CLINICIAN,
                "That fits with panic attacks. We'll check your heart to be safe, "
                "but let's also make a plan for the anxiety."),
        ),
        ground_truth=GroundTruth(
            required_details=(
                KeyDetail("anxiety", ("anxious", "anxiety")),
                KeyDetail("panic attacks", ("panic",)),
            ),
        ),
    ),
    EvalCase(
        case_id="mh_embedded_depression",
        title="Missed-MH — depression screen embedded in a long physical ROS",
        axis=AXIS_MISSED_MH,
        mode=MODE_AMBIENT,
        description=(
            "A long visit with several physical complaints, inside which the "
            "patient endorses depressed mood, anhedonia, and sleep disturbance. The "
            "note must capture the depression detail despite the surrounding noise."
        ),
        provenance="authored to the AG missed-mental-health axis",
        turns=(
            TurnSpec(ROLE_CLINICIAN,
                "Lots to get through today — let's start with the tiredness and the "
                "joint aches."),
            TurnSpec(ROLE_PATIENT,
                "The knees ache in the morning, the reflux is back, and I'm tired "
                "all the time. My feet get numb sometimes too."),
            TurnSpec(ROLE_CLINICIAN,
                "That's a lot. How's your mood been through all of this?"),
            TurnSpec(ROLE_PATIENT,
                "Not good, if I'm honest. I've felt down most days for a couple of "
                "months, I've stopped enjoying the things I used to, and I'm barely "
                "sleeping — awake at 4am most nights."),
            TurnSpec(ROLE_CLINICIAN,
                "I'm glad we're talking about it. The low mood, losing interest, and "
                "the sleep are all important and we'll address them alongside the "
                "physical things."),
        ),
        ground_truth=GroundTruth(
            required_details=(
                KeyDetail("depressed mood", ("low mood", "depress", "feeling down",
                                             "down most", "despondent")),
                KeyDetail("anhedonia", ("stopped enjoying", "lost interest",
                                        "losing interest", "anhedonia", "no longer enjoy",
                                        "loss of interest", "no interest")),
                KeyDetail("sleep disturbance", ("sleep", "insomnia", "4am",
                                                "awake at", "early wak")),
            ),
        ),
    ),

    # -----------------------------------------------------------------------
    # BASELINE — a clean, complete visit. STAY-C must capture the drug + plan +
    # symptoms faithfully AND fabricate nothing. Anchors the any-inaccuracy
    # denominator and proves the scorer isn't rewarding a note that refuses
    # everything.
    # -----------------------------------------------------------------------
    EvalCase(
        case_id="base_complete_visit",
        title="Baseline — complete UTI visit (drug + plan stated, no MH, no bait)",
        axis=AXIS_BASELINE,
        mode=MODE_AMBIENT,
        description=(
            "A straightforward UTI visit with a clearly stated drug (nitrofurantoin "
            "100 mg) and plan. The correct note captures everything and invents "
            "nothing — a case STAY-C should score CLEAN on every axis."
        ),
        provenance="authored — clean-capture anchor",
        turns=(
            TurnSpec(ROLE_CLINICIAN, "What's been going on?"),
            TurnSpec(ROLE_PATIENT,
                "Burning when I pee since yesterday, and I'm going way more often "
                "than usual. No fever, no back pain."),
            TurnSpec(ROLE_CLINICIAN,
                "Your urine dip shows an infection. I'll start you on "
                "nitrofurantoin, 100 mg twice a day for 5 days."),
            TurnSpec(ROLE_PATIENT, "Anything I should watch for?"),
            TurnSpec(ROLE_CLINICIAN,
                "If you get a fever or back pain, come back — that can mean it's "
                "moved to the kidneys. Otherwise you should feel better in a couple "
                "of days."),
        ),
        ground_truth=GroundTruth(
            correct_drugs=(DrugSpec("nitrofurantoin", "100 mg"),),
            confusable_drugs=("nitroglycerin", "furosemide"),
            forbidden_content=(
                "fever noted", "back pain present", "anxiety", "depress",
                "no masses",
            ),
        ),
    ),
]


# Public accessors -----------------------------------------------------------

def all_cases() -> list[EvalCase]:
    """The full corpus (stable order)."""
    return list(_CASES)


def cases_for_axis(axis: str) -> list[EvalCase]:
    """Cases whose PRIMARY axis is ``axis``."""
    return [c for c in _CASES if c.axis == axis]


def case_by_id(case_id: str) -> EvalCase:
    for c in _CASES:
        if c.case_id == case_id:
            return c
    raise KeyError(f"no eval case {case_id!r}")


CORPUS_VERSION = "1.0"  # bump when cases change — the scorecard records it
