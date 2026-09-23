"""Use the scraper straight from Python — no server needed.

    python main.py

Every function returns the same JSON the API does; results are written to
output/*.json. See README.md for the full endpoint list.
"""
import json
import os

from reddit import discovery, posts, refs, subreddits

os.makedirs("output", exist_ok=True)


def save(name, data):
    path = os.path.join("output", name)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"saved {path}")


if __name__ == "__main__":
    # a post id, t3_ fullname or any reddit.com post link — with its comment tree
    save("post_z1c9z_comments.json", posts.comments(refs.resolve_post("z1c9z"), sort="top"))

    # a subreddit name, r/name or link — hot / new / top / rising / controversial
    save("technology_top_week.json", subreddits.posts(refs.resolve_subreddit("technology"), sort="top", time="week"))

    # search posts across Reddit (Reddit's search syntax works)
    save("search_raspberry_pi.json", discovery.search_posts("raspberry pi"))
