import requests

BASE = "http://localhost:8000"
REFUSAL = "I couldn't find this in your documents."
OUTPUT_FILE = "eval_result.txt"

# Each case: (category, question, key words, file, expected pages, must_refuse)
# Key words are matched case-insensitively against the answer.
CASES = [
    # Water purifier manual
    ("manual", "What is the capacity of the AquaPure 3000?", ["7 litre"], "water_purifier_manual.pdf", [1], False),
    ("manual", "How many filtration stages does it have?", ["5"], "water_purifier_manual.pdf", [1], False),
    ("manual", "What is the flow rate?", ["12 litre"], "water_purifier_manual.pdf", [1], False),
    ("manual", "What is the power rating?", ["24"], "water_purifier_manual.pdf", [1], False),
    ("manual", "How long is the warranty on the RO membrane?", ["1 year"], "water_purifier_manual.pdf", [1], False),
    ("manual", "How often should the sediment filter be replaced?", ["6 month"], "water_purifier_manual.pdf", [1, 2], False),
    ("manual", "How much does an RO membrane cost?", ["1200"], "water_purifier_manual.pdf", [1, 2], False),
    ("manual", "How often is the UV lamp replaced?", ["12 month"], "water_purifier_manual.pdf", [1, 2], False),
    ("manual", "The water tastes salty. What is the cause?", ["RO membrane"], "water_purifier_manual.pdf", [2], False),
    ("manual", "The red light is blinking. What should I do?", ["5 minute"], "water_purifier_manual.pdf", [2], False),
    ("manual", "How long should I wait after switching on before drinking?", ["20 minute"], "water_purifier_manual.pdf", [1], False),
    ("manual", "How far should the unit be from the tap?", ["1 metre"], "water_purifier_manual.pdf", [1], False),
    ("manual", "What should I do if the flow rate is low?", ["sediment"], "water_purifier_manual.pdf", [2], False),
    ("manual", "Can I run the unit without water?", ["not"], "water_purifier_manual.pdf", [1, 2], False),
    ("manual", "How many screws are used for mounting?", ["four"], "water_purifier_manual.pdf", [1], False),

    # Hostel rules
    ("hostel", "What is the monthly fee for a shared room?", ["4,800"], "hostel_rules.pdf", [1], False),
    ("hostel", "What is the security deposit?", ["10,000"], "hostel_rules.pdf", [1], False),
    ("hostel", "What is the late fee and when does it apply?", ["100"], "hostel_rules.pdf", [1], False),
    ("hostel", "What time do the gates close on weekends?", ["10:30"], "hostel_rules.pdf", [1], False),
    ("hostel", "Can guests stay overnight?", ["not"], "hostel_rules.pdf", [1], False),
    ("hostel", "When is room inspection?", ["sunday"], "hostel_rules.pdf", [1], False),
    ("hostel", "How much notice is needed before leaving the hostel?", ["30 day"], "hostel_rules.pdf", [1], False),
    ("hostel", "Who is the warden and what is her extension?", ["214"], "hostel_rules.pdf", [1], False),
    ("hostel", "What is the emergency number?", ["022-555-0199"], "hostel_rules.pdf", [1], False),
    ("hostel", "Can I cook in my room?", ["prohibited"], "hostel_rules.pdf", [1], False),
    ("hostel", "What is the gate closing time on Tuesdays?", ["9:30"], "hostel_rules.pdf", [1], False),

    # Photosynthesis notes
    ("notes", "Where does photosynthesis take place?", ["chloroplast"], "photosynthesis_notes.pdf", [1], False),
    ("notes", "What are the two stages of photosynthesis called?", ["light-dependent"], "photosynthesis_notes.pdf", [1], False),
    ("notes", "Why do leaves look green?", ["reflect"], "photosynthesis_notes.pdf", [1], False),
    ("notes", "What are the three limiting factors?", ["light intensity"], "photosynthesis_notes.pdf", [1], False),

    # Must refuse: not in any document
    ("refusal", "What is the warranty of the Philips water purifier?", [], None, None, True),
    ("refusal", "What is the fee for a double room?", [], None, None, True),
    ("refusal", "What is the capital of France?", [], None, None, True),
    ("refusal", "What is the boiling point of water in the notes?", [], None, None, True),
    ("refusal", "Who is the principal of the school?", [], None, None, True),
]

def ask(question):
    resp = requests.post(f"{BASE}/chat", json={"question": question}, timeout=120)
    resp.raise_for_status()
    return resp.json()


def main():
    lines = []

    def out(text=""):
        print(text)
        lines.append(text)

    stats = {}
    for category, question, keys, file_name, pages, *rest in CASES:
        must_refuse = rest[-1] if rest else False
        bucket = stats.setdefault(category, {"total": 0, "passed": 0})
        bucket["total"] += 1
        try:
            data = ask(question)
        except Exception as exc:
            out(f"ERROR  [{category}] {question} -> {exc}")
            continue

        answer = data.get("answer", "")
        sources = data.get("sources", [])

        if must_refuse:
            ok = answer.strip() == REFUSAL and not sources
            out(f"{'PASS' if ok else 'FAIL'}  [{category}] {question}  refused={answer.strip() == REFUSAL} sources={len(sources)}")
        else:
            key_ok = all(k.lower() in answer.lower() for k in keys)
            page_ok = any(
                s.get("filename") == file_name and s.get("page_number") in pages
                for s in sources
            )
            ok = key_ok and page_ok
            out(f"{'PASS' if ok else 'FAIL'}  [{category}] {question}  key={key_ok} page={page_ok}")
            if not ok:
                out(f"      answer: {answer[:200]}")
                out(f"      cited:  {[(s.get('filename'), s.get('page_number')) for s in sources]}")

        bucket["passed"] += ok

    total = len(CASES)
    passed = sum(b["passed"] for b in stats.values())
    out()
    out("By category:")
    for name, b in stats.items():
        out(f"  {name:<10} {b['passed']} of {b['total']}")
    out()
    out(f"Overall: {passed} of {total} passed")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nSaved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
