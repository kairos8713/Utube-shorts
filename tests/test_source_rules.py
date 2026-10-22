"""Decision #5: unsuitable sources are filtered in discovery (code rules before any model call), clip
permission comes only from explicit description text, saturation counts real clip channels."""
import pytest

import source_rules as rules
import sources_manager as sm
from helpers import sector
from llm_client import set_llm
from llm_mock import MockLLM
from models import Source

PODCAST = "https://www.youtube.com/@GoodTalk"
BAND = "https://www.youtube.com/@QueenBandOfficial"          # Queen-like music channel
REACT = "https://www.youtube.com/@ReactCentral"
TEDX = "https://www.youtube.com/@TEDx"


class DiscoveryYT:
    """Four channels: a talk podcast, a band (category Music), a reaction channel, TEDx."""

    channels = {
        PODCAST: ("Good Talk Podcast", "UCgoodtalk01", "Long conversations about the mind. Clips welcome!",
                  [f"Episode {i}: why habits stick" for i in range(15)], "Education"),
        BAND: ("Queen Band", "UCqueenband01", "Official channel of the band.",
               [f"Live at Wembley part {i} (full concert)" for i in range(15)], "Music"),
        REACT: ("React Central", "UCreact00001", "We watch the internet so you don't have to.",
                [f"Reacting to viral TikToks #{i}" if i % 2 else f"Therapist explains episode {i}" for i in range(15)],
                "Entertainment"),
        TEDX: ("TEDx Talks", "UCsT0YIqwnpJCM-mx7-gSA4Q", "TEDx talks.",
               [f"The psychology of change | Speaker {i} | TEDxCity" for i in range(15)], "Education"),
    }

    def __init__(self):
        self.category_calls, self.searches = [], []

    def search(self, query, n=50):
        self.searches.append(query)
        if query.startswith("good talk "):                              # saturation searches
            return [{"id": f"s{i}", "title": f"Good Talk best moment {i}",
                     "channel_url": f"https://www.youtube.com/@clipper{i % 4}"} for i in range(8)] + \
                   [{"id": "x", "title": "unrelated", "channel_url": "https://www.youtube.com/@other"}]
        return [{"id": f"v{k}", "title": "ep", "duration": 3600, "channel_url": url}
                for k, url in enumerate(self.channels)]

    def channel_videos(self, url, n=15):
        title, cid, desc, titles, _ = self.channels[url]
        return {"channel": title, "channel_id": cid, "channel_follower_count": 100000, "description": desc,
                "uploader_id": "@" + url.rsplit("@", 1)[-1],
                "entries": [{"id": f"{cid}{i}", "title": t, "duration": 3000, "view_count": 50000,
                             "timestamp": 1_790_000_000 + i * 86400} for i, t in enumerate(titles[:n])]}

    def video_categories(self, video_url):
        self.category_calls.append(video_url)
        return [next(c[4] for c in self.channels.values() if c[1] in video_url)]

    def speech_sample(self, video_url):
        return 0.9, "en"

    def delay(self):
        pass


@pytest.fixture
def llm():
    m = MockLLM()
    set_llm(m)
    yield m
    set_llm(None)


def test_music_reaction_and_tedx_are_filtered_before_qwen(db, llm):
    sec = sector(db, "Psychology")
    yt = DiscoveryYT()
    sm.discover_niche(db, sec, yt, llm, max_candidates=4)
    st = {s.channel_url: s for s in db.query(Source).all()}
    assert st[BAND].status == "REJECTED" and "music channel" in st[BAND].status_reason
    assert st[REACT].status == "REJECTED" and "reaction/compilation format" in st[REACT].status_reason
    assert st[TEDX].status == "REJECTED" and "excluded publisher" in st[TEDX].status_reason
    assert st[PODCAST].status == "TRIAL"
    assert not any("UCsT0YIqwnpJCM" in u for u in yt.category_calls)       # listed publisher: no YouTube request
    assessed = [c["messages"][1]["content"] for c in llm.calls if c["task"] == "source_assessment"]
    assert len(assessed) == 1 and "why habits stick" in assessed[0]          # only the podcast reached Qwen
    assert not any(w in assessed[0] for w in ("Wembley", "TikTok", "TEDx"))
    assert assessed[0].count("Episode ") == 15 and "<<<DATA RECENT_VIDEOS" in assessed[0]   # last 15 videos


def test_clip_permission_only_from_explicit_text(db, llm):
    sec = sector(db, "Psychology")
    sm.discover_niche(db, sec, DiscoveryYT(), llm, max_candidates=4)
    src = db.query(Source).filter_by(channel_url=PODCAST).one()
    assert src.clip_permission == rules.PERMISSION_ALLOWED                     # "Clips welcome!"
    assert sm.resolve_permission("Weekly talks about money.", {"clip_permission": "unknown"}) is None
    # the model claims permission but its quote is not in the description → unknown, not a number
    assert sm.resolve_permission("Weekly talks about money.", {"clip_permission": "allowed",
                                                              "clip_permission_evidence": "clips are welcome"}) is None
    assert sm.resolve_permission("Use our clips freely, just credit us.",
                                 {"clip_permission": "allowed",
                                  "clip_permission_evidence": "Use our clips freely"}) == rules.PERMISSION_ALLOWED
    assert rules.permission_from_text("All videos licensed CC BY-NC-ND 4.0")[0] == rules.PERMISSION_FORBIDDEN


def test_unknown_permission_is_left_out_of_the_score():
    stats = {"avg_views": 1_000_000}
    a = {"niche_fit": 1.0}
    unknown = sm.score_candidate(stats, a, speech=1.0, sat=0.0, permission=None)
    assert unknown == 1.0                                         # rescaled, not punished with a made-up 0.3
    assert sm.score_candidate(stats, a, 1.0, 0.0, permission=0.0) == 0.85


def test_saturation_counts_distinct_clip_channels_by_normalized_name(db, llm):
    sec = sector(db, "Psychology")
    yt = DiscoveryYT()
    sm.discover_niche(db, sec, yt, llm, max_candidates=4)
    assert {"good talk clips", "good talk podcast clips", "good talk shorts"} <= set(yt.searches)
    src = db.query(Source).filter_by(channel_url=PODCAST).one()
    assert src.saturation == 0.4 and "clip channels: 4" in src.status_reason    # @clipper0..3


def test_exclusion_list_and_name_normalisation():
    assert rules.excluded_publisher("TED-Ed")
    assert rules.excluded_publisher("Bloomberg Television")
    assert rules.excluded_publisher("Some Artist - Topic")
    assert rules.excluded_publisher("Whatever", channel_id="UCAuUUnT6oDeKwE6v1NGQxug")      # TED by id
    assert rules.excluded_publisher("Jay Shetty Podcast") is None
    assert rules.excluded_publisher("Ted Lasso Fans") is None                                  # TED is case-sensitive
    assert rules.normalized_name("The Money Guy Show") == "money guy"
    assert rules.mentions_name("Money Guy on index funds", "money guy")


def test_review_script_proposes_but_changes_nothing(db):
    import hashlib
    import importlib.util
    import os
    from config import settings
    from helpers import source
    sec = sector(db, "Psychology")
    t = source(db, url="https://www.youtube.com/@TEDx", status="TRIAL", sector_obj=sec, title="TEDx Talks")
    r = source(db, url="https://www.youtube.com/@ReactCentral", status="TRIAL", sector_obj=sec, title="React Central")
    r.recent_titles = '["Reacting to viral TikToks 1", "Reacting to viral TikToks 2", "Real talk 3"]'
    ok = source(db, url="https://www.youtube.com/@GoodTalk", status="ACTIVE", sector_obj=sec, title="Good Talk Podcast")
    ok.description = "Long conversations. Clips welcome!"
    db.commit()
    path = settings.DATABASE_URL[len("sqlite:///"):]
    before = hashlib.sha256(open(path, "rb").read()).hexdigest()
    spec = importlib.util.spec_from_file_location("review_sources", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "review_sources.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    res = {x["id"]: x for x in mod.main(["--db", path])}
    assert res[t.id]["propose"] == "BLACKLIST" and "excluded publisher" in res[t.id]["reason"]
    assert res[r.id]["propose"] == "BLACKLIST" and "reaction" in res[r.id]["reason"]
    assert res[ok.id]["propose"] == "" and res[ok.id]["clip_permission_text"] == rules.PERMISSION_ALLOWED
    assert hashlib.sha256(open(path, "rb").read()).hexdigest() == before        # read-only
    assert db.get(Source, t.id).status == "TRIAL"
