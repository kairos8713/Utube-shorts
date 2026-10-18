import re
import json

CJK_PATTERN = re.compile(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]')

EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001F9FF"  # symbols & pictographs
    "\U00002600-\U000027BF"   # misc symbols
    "\U0001F000-\U0001F2FF"
    "\U0001FA00-\U0001FA6F"
    "]+", flags=re.UNICODE
)


def cjk_validate_and_extract_full(qwen_response, max_retries=3):
    """Extract JSON from Qwen response, tolerating prose preamble + markdown fences."""
    text = qwen_response.strip()
    
    if CJK_PATTERN.search(text):
        raise ValueError(f"CJK characters detected in Qwen response: {text[:100]}")
    
    # Strategy 1: Look for ```json ... ``` fenced block
    fence_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if fence_match:
        json_str = fence_match.group(1)
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass  # fall through to next strategy
    
    # Strategy 2: Find the first { and matching } using bracket counting
    start = text.find('{')
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    json_str = text[start:i+1]
                    try:
                        return json.loads(json_str)
                    except json.JSONDecodeError:
                        break
    
    # Strategy 3: Direct parse (clean response)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON from Qwen: {e}\nResponse: {text[:300]}")


def cjk_validate_and_extract(qwen_response, key, max_retries=3):
    """Returns the value at `key`. Raises if CJK or missing key."""
    parsed = cjk_validate_and_extract_full(qwen_response)
    if key not in parsed:
        raise KeyError(f"Key '{key}' missing in Qwen response: {parsed}")
    return parsed[key]


def _safe_extract(qwen_call_fn, prompt, key=None, default=None, max_retries=2):
    """Try Qwen call up to max_retries times. Return default on failure."""
    for attempt in range(max_retries):
        try:
            result = qwen_call_fn(prompt)
            if key:
                return cjk_validate_and_extract(result, key=key)
            return cjk_validate_and_extract_full(result)
        except (ValueError, KeyError) as e:
            print(f"[QWEN] Attempt {attempt+1} failed: {e}")
            if attempt == max_retries - 1:
                print(f"[QWEN] Using default for key={key}")
                return default
    return default


def generate_hook_text(context_str, niche, qwen_call_fn):
    prompt = f"""You are writing a VIRAL hook banner for a YouTube Short.

VIDEO CONTEXT:
{context_str}

NICHE: {niche}

This banner shows in the FIRST 3 SECONDS. If it's boring, the viewer leaves.

WRITE A HOOK THAT MAKES SOMEONE STOP SCROLLING.

REQUIREMENTS:
- 3-7 words MAX
- ALL CAPS
- One of these proven viral patterns:
  • Shock claim: "BANKS HATE THIS TRICK" / "THIS IS ILLEGAL"
  • Contradiction: "RICH PEOPLE NEVER DO THIS" / "YOU'VE BEEN LIED TO"
  • Authority diss: "EXPERTS ARE WRONG"
  • Curiosity gap: "WHAT NOBODY TELLS YOU"
  • Pattern interrupt: "STOP SAVING MONEY"
  • Stakes: "YOU'LL LOSE EVERYTHING"

BAD HOOKS (do not write):
- "TIPS FOR SUCCESS" (vague)
- "MONEY ADVICE" (no stakes)  
- "SOMETHING YOU SHOULD KNOW" (passive)
- "GREAT FINANCIAL ADVICE" (puffery)

The hook must CONNECT to what's actually said in the video. Reference the EMPHASIZED words from the context above.

Output ONLY a valid JSON object. No prose. No markdown. No explanation. No code fences. Start your response with {{ and end with }}. Nothing else.

{{"hook_text": "<3-7 word ALL CAPS hook>"}}
"""
    return _safe_extract(qwen_call_fn, prompt, key="hook_text", default="WATCH THIS NOW")


def generate_insight(context_str, hook_text, niche, qwen_call_fn):
    prompt = f"""You are writing the INSIGHT banner for seconds 5-10 of a viral Short.

VIDEO CONTEXT:
{context_str}

HOOK FROM BEAT 1: "{hook_text}"
NICHE: {niche}

This banner DELIVERS on the hook's promise with a specific, shocking fact.

REQUIREMENTS:
- 10-18 words (NOT shorter — needs weight)
- Sentence case
- MUST include ONE of these:
  • A specific number/percentage/dollar amount
  • A specific timeframe ("in 6 months", "by age 30")
  • A specific group ("the top 1%", "people who retire at 40")
  • A specific contrast ("X does Y, but smart people do Z")
- NO hedge words: "might", "maybe", "could be", "sometimes"
- MUST sound like a definitive insider secret

GOOD examples:
- "The top 1% saves 73% of every raise — most people save 0%"
- "Investing $200/month for 30 years beats most six-figure salaries"
- "Buying a home before 25 is the #1 wealth killer in Gen Z"

BAD examples (DO NOT WRITE):
- "Saving money is important" (no stake)
- "Investing is risky but rewarding" (cliché)
- "Be careful with your money" (advice without info)

Output ONLY a valid JSON object. No prose. No markdown. No code fences. Start with {{ and end with }}. Nothing else.

{{"insight_text": "<10-18 word insight with specific number/group/contrast>"}}
"""
    return _safe_extract(qwen_call_fn, prompt, key="insight_text", default="Surprising data revealed")


def generate_infoboxes(context_str, hook_text, niche, qwen_call_fn):
    prompt = f"""Write TWO ticker banners for a viral Short. These appear during the climax.

VIDEO CONTEXT:
{context_str}

HOOK: "{hook_text}"
NICHE: {niche}

Each ticker is a SHOCK STAT or TRIBAL CALLOUT.

REQUIREMENTS for each:
- 5-10 words
- Starts with an emoji (◉ ⚡ 🔥 📊 💰 ⚠️ 🚨 💸 ⏰)
- ONE of these patterns:
  • Shock stat: "💰 73% of millionaires do this daily"
  • Tribal call: "🔥 Gen Z knows this. Boomers don't."
  • Time urgency: "⏰ You have 5 years to figure this out"
  • Authority diss: "⚠️ Your financial advisor won't tell you"
  • Loss aversion: "💸 Lose $47k by ignoring this"
- The two tickers must HIT DIFFERENT EMOTIONS
  (ticker_1 = shock stat with number; ticker_2 = tribal/urgency callout)

BAD (do not write):
- "◉ Save 50%" (no context — 50% of what?)
- "⚡ Risk is high" (no stakes, no specifics)

Output ONLY a valid JSON object. No prose. No markdown. No code fences. Start with {{ and end with }}. Nothing else.

{{"ticker_1": "<emoji + 5-10 word shock stat>", "ticker_2": "<emoji + 5-10 word tribal callout>"}}
"""
    parsed = _safe_extract(qwen_call_fn, prompt, default={"ticker_1": "KEY INSIGHT", "ticker_2": "IMPORTANT FACT"})
    # Strip emojis — but fall back to original if stripping wipes the text
    for k in ("ticker_1", "ticker_2"):
        if k in parsed and parsed[k]:
            original = parsed[k]
            stripped = EMOJI_PATTERN.sub("", original).strip()
            # If stripping left less than 5 chars, keep original (emoji-heavy response)
            if len(stripped) >= 5:
                parsed[k] = stripped.upper()
            else:
                parsed[k] = original.strip().upper()
            # Final safety: ensure at least 3 readable chars
            if len(parsed[k].strip()) < 3:
                parsed[k] = "KEY INSIGHT" if k == "ticker_1" else "WORTH KNOWING"
    return parsed.get("ticker_1", "KEY INSIGHT"), parsed.get("ticker_2", "WORTH KNOWING")



def generate_outro(context_str, hook_text, niche, qwen_call_fn):
    prompt = f"""Write the OUTRO for a viral Short. This is the LAST IMPRESSION — it must drive engagement.

VIDEO CONTEXT:
{context_str}

HOOK: "{hook_text}"
NICHE: {niche}

You will write TWO outputs:

1. JUDGMENT — A definitive, quotable verdict (10-16 words):
   PATTERNS:
   • "The data is clear: <verdict>"
   • "Bottom line — <action> or <consequence>"
   • "If you remember one thing, remember this: <truth>"
   • "Smart people <X>. Everyone else <Y>."
   
   GOOD: "Bottom line — invest in skills, not stuff, or stay broke forever."
   BAD: "Investing without research is risky." (too short, no stakes)

2. CTA_QUESTION — A high-engagement comment driver (6-14 words):
   PICK ONE of these styles (vary across videos):
   
   Style A — ACTION COMMAND:
   • "Save this if it hit you."
   • "Share with someone who needs to hear this."
   • "Tag a friend who's still making this mistake."
   
   Style B — OPINION SPLIT:
   • "Hot take or hard truth? Drop your verdict."
   • "Genius move or terrible advice? Comment below."
   • "Agree or are you the exception? Tell me."
   
   Style C — CURIOSITY:
   • "Did anyone teach you this? Comment yes or no."
   • "What age did YOU learn this? Drop it below."
   
   Style D — IDENTITY:
   • "Type 'YES' if you're done playing small."
   • "Comment 'BUILT' if this is you."

   PICK ONE STYLE. NO "should/would" YES/NO QUESTIONS.

3. VOICE_RECOMMENDATION — Pick voice based on niche:
   • Personal Finance / Business → "am_adam" (authority male)
   • Tech / AI → "am_eric" (clean male)
   • Education / Psychology → "af_bella" (warm female)
   • Health / Food / Cooking → "af_sky" (friendly female)
   • Default → "am_adam"

Output ONLY a valid JSON object. No prose. No markdown. No code fences. Start with {{ and end with }}. Nothing else.

{{"judgment": "<10-16 word definitive verdict>", "cta_question": "<6-14 word high-engagement CTA, NO yes/no>", "voice_recommendation": "<voice id>"}}
"""
    parsed = _safe_extract(qwen_call_fn, prompt, default={"judgment": "The data is clear.", "cta_question": "What do you think?", "voice_recommendation": "af_sky"})
    return parsed.get("judgment", "The data is clear."), parsed.get("cta_question", "What do you think?"), parsed.get("voice_recommendation", "af_sky")


def generate_caption_hashtags(hook_text, judgment, niche, source_channel, qwen_call_fn):
    prompt = f"""Write the CAPTION and HASHTAGS for a viral YouTube Short.

HOOK: "{hook_text}"
JUDGMENT: "{judgment}"
NICHE: {niche}
SOURCE CHANNEL: {source_channel}

CAPTION FORMULA:
Line 1: A bold, contrarian, scroll-stopping statement (echoes the hook but rephrased)
Line 2: A specific reason or insider truth that delivers value
Line 3: A community-building CTA — invites reaction, not just consumption
Optional: 1-2 strategic emojis (NOT in every sentence)

Total: 3-4 sentences, 40-80 words.

GOOD example:
"Most people will work 40 years for someone else's dream. Here's the difference: the top 1% don't trade time for money — they build assets that pay them while they sleep. 💰 Which side are you on? Drop your move below."

BAD examples:
- "Great tip about investing. Watch the full video!" (no substance)
- "Money is important. Save and invest." (cliché, no specificity)

HASHTAGS — EXACTLY 5:
- 1x viral broad: #shorts OR #fyp
- 2x niche-specific (e.g. #PersonalFinance, #MoneyTips, #WealthBuilding)
- 1x audience tribal (e.g. #GenZMoney, #MillennialFinance, #SmallBusiness)
- 1x unique angle from the video content

Output ONLY a valid JSON object. No prose. No markdown. No code fences. Start with {{ and end with }}. Nothing else.

{{"caption": "<3-4 sentence viral caption, 40-80 words>", "hashtags": ["#tag1", "#tag2", "#tag3", "#tag4", "#tag5"]}}
"""
    parsed = _safe_extract(qwen_call_fn, prompt, default={"caption": "", "hashtags": ["#shorts", "#fyp"]})
    return parsed.get("caption", ""), parsed.get("hashtags", ["#shorts", "#fyp"])


def generate_youtube_title(context_str, hook_text, judgment, niche, qwen_call_fn):
    """Generate a unique, clickbait YouTube Shorts title (max 70 chars)."""
    context_excerpt = context_str[:500] if context_str else ""

    prompt = f"""You are a viral YouTube Shorts title writer. Generate ONE title.

CONTEXT:
- Hook shown in video: "{hook_text}"
- Key message: "{judgment}"
- Niche: {niche}
- Video topic snippets: {context_excerpt}

RULES:
- MAX 70 characters
- Sentence case (not ALL CAPS)
- Specific + curiosity gap (numbers, names, contrast)
- NEVER reuse the hook verbatim — reframe it differently
- NO emojis, NO hashtags, NO quotation marks
- Different every time — vary the pattern

GOOD EXAMPLES:
- "Why 90% of Traders Lose Money (And What Rich People Do)"
- "The One Banking Trick Nobody Tells You"
- "Alex Hormozi Explains the Retirement Math Nobody Understands"

BAD EXAMPLES (do NOT do these):
- "EXPERTS ARE WRONG" (all caps hook copy)
- "Watch this amazing video" (generic)
- "You won't believe #shorts" (has hashtag)

Output ONLY a valid JSON object. No prose. No markdown. No code fences. Start with {{{{ and end with }}}}. Nothing else.

{{{{"title": "your title here"}}}}"""

    result = _safe_extract(qwen_call_fn, prompt, key="title", default="")
    # Fallback: never return empty
    if not result or len(result.strip()) < 10:
        result = f"{judgment[:50]}..." if judgment else "Watch This Now"
    return result.strip()[:70]


def generate_all_outputs(context_bundle, niche, source_channel, qwen_call_fn):
    """Run all 5 Qwen calls sequentially. Returns dict of all outputs."""
    from multimodal_context import format_for_qwen
    context_str = format_for_qwen(context_bundle)
    
    outputs = {}
    
    # Sequential calls (NPU is single-threaded)
    print("[QWEN B1] Generating hook text...")
    outputs["hook_text"] = generate_hook_text(context_str, niche, qwen_call_fn)
    print(f"[QWEN B1] hook: {outputs['hook_text']}")
    
    print("[QWEN B2] Generating insight...")
    outputs["technical_insight"] = generate_insight(context_str, outputs["hook_text"], niche, qwen_call_fn)
    print(f"[QWEN B2] insight: {outputs['technical_insight']}")
    
    print("[QWEN B3] Generating infoboxes...")
    t1, t2 = generate_infoboxes(context_str, outputs["hook_text"], niche, qwen_call_fn)
    outputs["infobox_1"] = t1
    outputs["infobox_2"] = t2
    print(f"[QWEN B3] tickers: {t1} | {t2}")
    
    print("[QWEN B4] Generating outro...")
    j, cta, voice = generate_outro(context_str, outputs["hook_text"], niche, qwen_call_fn)
    outputs["judgment"] = j
    outputs["cta_question"] = cta
    outputs["voice_recommendation"] = voice
    print(f"[QWEN B4] judgment: {j}")
    print(f"[QWEN B4] cta: {cta}")
    
    print("[QWEN B5] Generating caption + hashtags...")
    cap, tags = generate_caption_hashtags(outputs["hook_text"], outputs["judgment"], niche, source_channel, qwen_call_fn)
    outputs["caption"] = cap
    outputs["hashtags"] = tags
    print(f"[QWEN B5] caption: {cap[:60]}...")
    
    print("[QWEN B6] Generating YouTube title...")
    outputs["title"] = generate_youtube_title(
        context_str,
        hook_text=outputs.get("hook_text", ""),
        judgment=outputs.get("judgment", ""),
        niche=niche,
        qwen_call_fn=qwen_call_fn,
    )
    print(f"[QWEN B6] title: {outputs['title']}")
    
    return outputs


if __name__ == "__main__":
    # Mock test — use a fake qwen_call_fn that returns hardcoded JSON
    def mock_qwen(prompt):
        if "HOOK" in prompt and "3-7 words" in prompt:
            return '{"hook_text": "THIS CHANGES EVERYTHING", "reasoning": "test"}'
        if "INSIGHT" in prompt:
            return '{"insight_text": "GPU demand outpacing supply by 3x this quarter", "key_number": "3x"}'
        if "ticker" in prompt.lower():
            return '{"ticker_1": "◉ Market cap hit $2T", "ticker_2": "⚡ Q4 beat by 34%"}'
        if "JUDGMENT" in prompt:
            return '{"judgment": "The data confirms a historic inflection point", "cta_question": "Will AI stocks keep climbing?", "voice_recommendation": "af_sky"}'
        if "CAPTION" in prompt:
            return '{"caption": "AI stocks just hit a milestone. The data shows we are entering a new era.", "hashtags": ["#AIStocks", "#Investing", "#shorts", "#fyp", "#TechAnalysis"]}'
        return "{}"
    
    # Mock context
    from multimodal_context import build_context_bundle, format_for_qwen
    # We will just pass an empty mock_bundle for testing the formatting
    mock_bundle = {"clip_duration": 20.0, "enriched_transcript": [], "energy_map": [], "pauses": [], "scene_changes": [], "audio_peak_time": 14.5}
    
    outputs = generate_all_outputs(mock_bundle, niche="Tech/AI", source_channel="TestChannel", qwen_call_fn=mock_qwen)
    
    print("\n=== ALL OUTPUTS ===")
    for k, v in outputs.items():
        print(f"  {k}: {v}")
