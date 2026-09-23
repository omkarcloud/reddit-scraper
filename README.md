# 🤖 Reddit Scraper

Reddit Scraper is a **free and open-source** scraper that gets you **unlimited** detailed Reddit data for free.

## ✨ What Can I Get?

- 🧵 **Any post with its full comment tree** — nested replies, scores, authors & flairs; export whole threads of up to 2,000 comments in one call
- 🔍 **Search 26B+ posts & comments** — posts, comments, media, communities & users with sort and time filters
- 🏘️ **All 100K+ active communities** — members, rules, wiki, pinned posts, similar subs & best posting hours
- 👤 **Any user's full profile** — karma, cake day, trophies, post & comment history, subreddits they moderate

## 🎥 Example: A Full Reddit Thread

```json
{
  "post": {
    "id": "z1c9z",
    "title": "I am Barack Obama, President of the United States -- AMA",
    "link": "https://www.reddit.com/r/IAmA/comments/z1c9z/i_am_barack_obama_president_of_the_united_states/",
    "created_at": "2012-08-29T20:01:36Z",
    "subreddit": { "name": "IAmA", "subscriber_count": 22477891 },
    "author": {
      "username": "PresidentObama",
      "flair": { "text": "Obama" }
    },
    "stats": { "score": 216179, "upvote_ratio": 0.68, "comment_count": 22719, "crosspost_count": 20 }
  },
  "sort": "top",
  "comments": [
    {
      "id": "c60mm41",
      "text": "Are you considering increasing funds to the space program?\n\nEdit: grammar",
      "author": { "username": "ormirian" },
      "created_at": "2012-08-29T20:05:05Z",
      "stats": { "score": 3042 },
      "replies": [
        {
          "id": "c60n05h",
          "depth": 1,
          "text": "Making sure we stay at the forefront of space exploration is a big priority for my administration. The passing of Neil Armstrong this week is a reminder of the…",
          "author": { "username": "PresidentObama" },
          "created_at": "2012-08-29T20:24:23Z",
          "stats": { "score": 2531 },
          "flags": { "is_submitter": true }
        }
      ]
    }
  ],
  "more_comments": { "count": 22283, "cursor": "eyJkZXB0aCI6MiwibGltaXQi…" }
}
```

*Trimmed for readability.*

## 🚀 Unlimited Free Reddit Data — Get It in 60 Seconds

1️⃣ Clone and install:
```bash
git clone https://github.com/omkarcloud/reddit-scraper
cd reddit-scraper
python -m pip install -r requirements.txt
```

2️⃣ Start the API:
```bash
python run.py
```

3️⃣ Get your first data:
```bash
curl "http://localhost:8000/posts/comments?post=z1c9z&sort=top&limit=3&depth=1"
```

```json
{
  "post": {
    "id": "z1c9z",
    "title": "I am Barack Obama, President of the United States -- AMA",
    "subreddit": { "name": "IAmA", "subscriber_count": 22477891 },
    "author": { "username": "PresidentObama" },
    "stats": { "score": 216183, "upvote_ratio": 0.68, "comment_count": 22719 }
  },
  "sort": "top",
  "comment_count": 3,
  "comments": [
    {
      "id": "c60o0iw",
      "author": { "username": "Biinaryy" },
      "stats": { "score": 5402 },
      "text": "Here is a collection of all the questions and answers:\n\nQuestion:…",
      "replies": []
    }
  ],
  "more_comments": { "count": 26832 }
}
```

All 40 endpoints are now live at `http://localhost:8000`.

## 📚 Endpoints

40 endpoints cover everything you need.

| Endpoint | Path | Returns |
|---|---|---|
| Post Comments | `/posts/comments` | A post with its full nested comment tree |
| Post Details | `/posts/details` | Everything about one post: body, author, score, media |
| Export Thread | `/posts/comments/export` | A whole thread as one flat list, up to 2,000 comments |
| Post Media | `/posts/media` | Original images, gallery items, videos with audio |
| Post Duplicates | `/posts/duplicates` | Every crosspost and repost of the same link |
| Posts Batch / Subreddits Batch / Users Batch | `/posts/batch`, `/subreddits/batch`, `/users/batch` | Up to 100 posts, subreddits or users in one call |
| Posts by URL | `/posts/by-url` | Every Reddit post that shared an external link |
| Comment Details | `/comments/details` | One comment with its parent comments |
| Autocomplete | `/search/autocomplete` | Typeahead for subreddits and user profiles |
| Search Posts / Comments / Media | `/search/posts`, `/search/comments`, `/search/media` | All of Reddit, sorted and filtered by time |
| Search Subreddits / Users | `/search/subreddits`, `/search/users` | Communities and accounts matching a keyword |
| Subreddit Posts | `/subreddits/posts` | Hot, new, top, rising or controversial posts |
| Subreddit Details | `/subreddits/details` | Members, description, sidebar, settings and rules |
| Search in Subreddit | `/subreddits/search` | Posts inside one subreddit, sorted and filtered |
| Subreddit Comments | `/subreddits/comments` | Newest comments, each with its post |
| Subreddit Insights | `/subreddits/insights` | Posts per day, best posting hours, top flairs and authors |
| Subreddit Rules / Wiki | `/subreddits/rules`, `/subreddits/wiki` | Rules, and any wiki page as Markdown and HTML |
| Similar Subreddits | `/subreddits/similar` | Communities Reddit recommends next to this one |
| Sticky Post | `/subreddits/sticky` | The pinned post with its top comments |
| Subreddit Flairs | `/subreddits/flairs` | Post flairs in use, ranked by popularity |
| Top / Popular / New Subreddits | `/subreddits/leaderboard`, `/subreddits/popular`, `/subreddits/new` | Leaderboard by weekly visitors, plus directories |
| User Details | `/users/details` | Karma, cake day, followers, bio and trophies |
| User Posts / Comments / Overview | `/users/posts`, `/users/comments`, `/users/overview` | Full post and comment history, sortable |
| User Insights / Active Subreddits | `/users/insights`, `/users/active-subreddits` | Where and when a user is most active |
| User Trophies / Moderated Subreddits | `/users/trophies`, `/users/moderated` | Trophy case, and every subreddit they moderate |
| Popular / All / Best Posts | `/feeds/popular`, `/feeds/all`, `/feeds/best` | r/popular in 16 countries, r/all, the front page |

## 🔍 Exploring Parameters

The same API is published on RapidAPI, and its playground is the easiest place to try parameters and see raw responses. Once a request looks right, run it locally for **unlimited free** data.

1. [Subscribe to the free plan](https://rapidapi.com/OmkarCloud/api/best-reddit-scraper-free-1000-calls/pricing) — 1,000 calls/month, no credit card.
2. [Try the endpoints in the playground](https://rapidapi.com/OmkarCloud/api/best-reddit-scraper-free-1000-calls/playground) — every param is pre-filled, so you see real data in one click.
3. Copy the generated code and replace `https://best-reddit-scraper-free-1000-calls.p.rapidapi.com` with `http://localhost:8000`. It will now run against your local API.

```python
import requests

# generated by the playground, host swapped for the local API
response = requests.get(
    "http://localhost:8000/posts/comments",
    params={"post": "z1c9z"},
)
print(response.json())
```

## 💬 Have Questions? We Have Answers.

You're a developer — we know how hard completing a project can be. So we offer full support: just message us and we'll reply ✅ with a solution within 1 working day.

[![Message Us on WhatsApp about Reddit Scraper](https://raw.githubusercontent.com/omkarcloud/assets/master/images/whatsapp-us.png)](https://api.whatsapp.com/send?phone=918178804274&text=I%20need%20help%20using%20the%20Reddit%20Scraper%20API.)

[![Ask Us by Email about Reddit Scraper](https://raw.githubusercontent.com/omkarcloud/assets/master/images/ask-on-email.png)](mailto:happy.to.help@omkar.cloud?subject=Help%20with%20Reddit%20Scraper%20API&body=I%20need%20help%20using%20the%20Reddit%20Scraper%20API.)

## ⚡ Popular Scrapers by Omkar Cloud

- [**Google Maps Scraper (3,100+ GitHub Stars)**](https://github.com/omkarcloud/google-maps-scraper) — type "dentists in New York", get every business as a ready-to-call lead list: phones, emails, websites & reviews. Up to 100K free leads/month.
- [**Threads Scraper**](https://github.com/omkarcloud/threads-scraper) — Threads posts, profiles, replies & search
- [**IMDb Scraper**](https://github.com/omkarcloud/imdb-scraper) — movies, TV shows, ratings, cast & box office
- [**G2 Scraper**](https://www.omkar.cloud/tools/g2-scraper) — G2 product details, ratings & AI-found contacts
- [**Website Email Contact Scraper**](https://www.omkar.cloud/tools/website-email-contact-scraper) — emails, phones & socials from any website
- [**AliExpress Scraper**](https://www.omkar.cloud/tools/aliexpress-scraper) — live product details, SKU variants, stock & shipping

## ⭐ Love It? [Star It ⭐!](https://github.com/omkarcloud/reddit-scraper)

Star the repo ⭐ and become my star hero!

It's just 1 click, but it means the world to me.

[![Star us on GitHub](https://raw.githubusercontent.com/omkarcloud/google-maps-scraper/master/screenshots/star-us.png)](https://github.com/omkarcloud/reddit-scraper)
