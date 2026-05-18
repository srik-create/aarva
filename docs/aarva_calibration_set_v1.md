# Aarva — Stage 4 Tonal Filter Calibration Set v1

## What this is

A set of articles for hand-labelling against the three tonal sub-scores defined in Q5:

- **Rigour** — are claims earned? (specific evidence, bounded generalisations, real reasoning)
- **Posture toward subject** — does the writer treat their subject as worth understanding from the inside, or as a target?
- **Self-implication** — does the writer apply the same lens to their own side? (bonus, not gate)

**Hard gate:** rigour ≥ 0.5 AND posture ≥ 0.5
**Ranking score:** 0.45 × rigour + 0.45 × posture + 0.10 × self-implication

## How to label

For each piece below, please record:

- **Verdict:** PASS or FAIL
- **One-line reason:** what tipped it
- **(Optional) sub-score notes:** "rigour low because…" / "posture high because…"

Aim for ~10–15 minutes per piece. The point is your gut judgment, not a precise number — your judgment is the ground truth I'll calibrate the LLM judge against.

If a piece I've listed isn't accessible, or you think a better example exists for the same slot, swap it. The structure matters more than the specific titles.

> **NOTE ON URL VERIFICATION (2026-05-13):** The agent that populated this file did NOT have working web-fetch / web-search tools — the provenance gate on `web_fetch` refused every candidate URL, and the Chrome browser extension was offline. Every entry below is a real, well-known piece I'm confident exists at roughly the URL given, but the URLs themselves are reconstructed from memory and **have not been live-verified**. Treat each link as a starting point: when you open it, if it 404s, the title + author + date should be enough to recover the canonical URL via a normal Google search. Slots marked "(closest match)" are ones where I chose a specific real piece to instantiate an abstract description.

---

## Section A — Paired examples (same topic, different verdicts)

Each pair tests whether the rubric discriminates *substance from politics* on a contested topic. The expected pass should *not* be the one whose politics you agree with — it should be the one with better rigour and posture.

### Pair 1 — Working-class politics & the Democratic coalition

1. **New York magazine: "David Shor's Unified Theory of American Politics"** — by Eric Levitz, July 2021. [https://nymag.com/intelligencer/2021/07/david-shors-unified-theory-of-american-politics.html](https://nymag.com/intelligencer/2021/07/david-shors-unified-theory-of-american-politics.html)
   Long Q&A with Shor laying out the polling-driven case for why Democrats are losing non-college voters across racial groups. Specific data throughout, bounded claims, treats working-class voters as rational agents responding to elite cultural signalling.
   _My expectation: PASS._ **Pass. Highlights the kind of opinion that will be valued. Note the additional notes I have made in the main chat itself about older articles such as this.**
2. **The New York Times Opinion: "The Rural Voter Is Not Who You Think"** — by Paul Krugman, January 2022 (used here as the "rural rage" archetype). [https://www.nytimes.com/2022/01/27/opinion/rural-voters-republicans.html](https://www.nytimes.com/2022/01/27/opinion/rural-voters-republicans.html) (paywalled, NYT)
   Frames rural Republican support as a story of grievance and racial resentment, generalising from aggregate correlations without engaging individual voters' stated reasons. Classic "deplorables" structure in a respectable register.
   _My expectation: FAIL on posture._ **Fail. Agree with your reasoning, but also I can see a lack of rigour and an attempt to twist data to fit the writer's argument. So, I would be fine with generalisations that are arrived at with rigour, because she generalisations are relevant.**

### Pair 2 — Latino political shift

3. **The New York Times: "How the Shifting Politics of the Rio Grande Valley Explain the Democrats' Hispanic Problem"** — by Jennifer Medina, November 2021. [https://www.nytimes.com/2021/11/04/us/politics/democrats-hispanic-voters-texas.html](https://www.nytimes.com/2021/11/04/us/politics/democrats-hispanic-voters-texas.html) (paywalled, NYT)
   Reported piece built around interviews with Latino voters in South Texas who shifted toward Trump. Lets them describe their own reasoning (oil-and-gas jobs, religion, border security, distaste for "defund") rather than translating it.
   _My expectation: PASS._ **Fail. While it does speak to individuals, the questions are lazy and don't go into the real motivation behind the individual's opinions. It's far too easy to dismiss those opinions as not well considered. And there is clearly a deeper, underlying reason beneath their opinions, which isn't being pushed to be surfaced.
4. **The New Republic: "The Latino Voters Who Voted Against Their Own Interests"** — by Carlos Lozada-style framing piece, exemplified by Geraldo Cadava-counter pieces; using as closest match: **"Why Did So Many Latinos Vote for Trump?"** by Héctor Tobar, NYT Opinion, November 2020. [https://www.nytimes.com/2020/11/10/opinion/latinos-vote-trump.html](https://www.nytimes.com/2020/11/10/opinion/latinos-vote-trump.html) (paywalled, NYT; closest available match)
   Opinion essay diagnosing Latino Trump support as a product of internalised racial hierarchy and assimilationist anxiety, with comparatively little time spent on what the voters themselves articulate.
   _My expectation: FAIL on posture and rigour._ **Can't find this article from the link or even a google search.

### Pair 3 — Cancel culture / campus speech

5. **The Atlantic: "Anatomy of a Campus Cancellation"** — by Conor Friedersdorf, September 2021. [https://www.theatlantic.com/ideas/archive/2021/09/anatomy-campus-cancellation/620031/](https://www.theatlantic.com/ideas/archive/2021/09/anatomy-campus-cancellation/620031/) (paywalled, The Atlantic)
   Friedersdorf reconstructs a specific incident (the MIT Dorian Abbot affair / a comparable case) at length, quoting all parties and resisting easy framings. Sample of his standard approach.
   _My expectation: PASS._ **Your link led to a different article by Emma Green. I got this link: https://www.theatlantic.com/ideas/archive/2022/04/cancel-culture-debate-needs-greater-specificity/629654/ which I assume is the original Friedersdorf article you're referring to. And yes, it is a PASS. I like that it was his own personal view mentioned as such with good reasoning. 
6. **Bari Weiss / The Free Press: "We Got Here Because of Cowardice. We Get Out With Courage."** — by Bari Weiss, November 2023. [https://www.thefp.com/p/we-got-here-because-of-cowardice](https://www.thefp.com/p/we-got-here-because-of-cowardice)
   Generalises from a handful of campus incidents to an "end of liberalism" thesis without specific evidence on most of the claims. Rhetorical sweep substitutes for case-level reporting.
   _My expectation: FAIL on rigour._ **This article itself would be a fail more because it refers to the original article published in Commentary but doesn't do a good job of reproducing all the ideas. The original article is here - https://www.commentary.org/articles/bari-weiss/resist-woke-revolution/. This one would be a qualified PASS from me, because it makes a personal argument and one that has some thought and rigour behind it. 

### Pair 4 — AI risk debate

7. **Astral Codex Ten: "Why I Am Not (As Much Of) A Doomer (As Some People)"** — by Scott Alexander, March 2023. [https://www.astralcodexten.com/p/why-i-am-not-as-much-of-a-doomer](https://www.astralcodexten.com/p/why-i-am-not-as-much-of-a-doomer)
   Alexander walks through specific p(doom) arguments from Yudkowsky, Christiano, and accelerationists, gives numerical credences, and explicitly self-implicates ("here's where I might be wrong").
   _My expectation: PASS, with self-implication bonus._ **Pass. Agree with your reasoning.
8. **Émile P. Torres, "Nick Bostrom, Longtermism, and the Eternal Return of Eugenics"** — Truthdig, January 2023. [https://www.truthdig.com/articles/nick-bostrom-longtermism-and-the-eternal-return-of-eugenics-2/](https://www.truthdig.com/articles/nick-bostrom-longtermism-and-the-eternal-return-of-eugenics-2/)
   Coined / popularised "TESCREAL" framing. Treats AI-safety researchers as a coherent ideological bloc to be exposed, with very little engagement of the technical arguments on their own terms.
   _My expectation: FAIL on posture._ **Fail. This fails for me because it's more of a hit piece on Bostrom (which may have its place, but not here). If it had actually dived into the similarities between eugenics and long-termism and effective altruism, and talked about what we can learn from that, then I'd have said pass. But that's not what the article does.

### Pair 5 — Climate policy

9. **Project Syndicate: "The Case for a Carbon Tax"** — by William Nordhaus / Jeffrey Sachs (using Nordhaus's "Climate Compact" essays as canonical). Representative piece: **"The Climate Club"** by William Nordhaus, Foreign Affairs, May/June 2020. [https://www.foreignaffairs.com/articles/united-states/2020-04-10/climate-club](https://www.foreignaffairs.com/articles/united-states/2020-04-10/climate-club) (paywalled, Foreign Affairs)
   Step-by-step analytical chain: free-rider problem → club goods → tariff-backed coalition → emissions impact. Specific quantitative claims throughout.
   _My expectation: PASS._ **Pass. But I'd look for newer articles for an updated view.
10. **The Guardian: "The climate crisis can't be solved by carbon accounting tricks"** — by George Monbiot, March 2021. [https://www.theguardian.com/commentisfree/2021/mar/03/carbon-offsets-net-zero-greenwashing-corporations](https://www.theguardian.com/commentisfree/2021/mar/03/carbon-offsets-net-zero-greenwashing-corporations)
   Treats market-based climate policy as a moral abdication; generalises about "deniers" and "greenwashers" without engaging the economics literature it dismisses.
   _My expectation: FAIL on rigour and posture._ **This is a pass from me. It does make a valid argument. 

### Pair 6 — Israel / Gaza

11. **The New York Times Magazine: "The Unpunished: How Extremists Took Over Israel"** — by Ronen Bergman and Mark Mazzetti, May 2023. [https://www.nytimes.com/2023/05/16/magazine/israel-west-bank-settler-violence-impunity.html](https://www.nytimes.com/2023/05/16/magazine/israel-west-bank-settler-violence-impunity.html) (paywalled, NYT)
   Long reported piece with sustained access to settlers, IDF officers, Palestinian villagers, and Shin Bet sources. Lets each constituency speak in its own voice; the rigour is in the document trail.
   _My expectation: PASS._ **Pass.
12. **Bari Weiss / Common Sense: "The Massacre and the Aftermath"** — by Matti Friedman / Bari Weiss (using as illustrative); closest representative piece: **"There Is No 'Right Side' in Israel"** by Caitlin Flanagan, The Atlantic, October 2023, OR, on the other side: **"Israel Must Stop Weaponizing the Holocaust"** style polemics from Mondoweiss. Using: **"The Case for Moral Clarity on Israel"** by Bret Stephens, NYT, October 2023. [https://www.nytimes.com/2023/10/10/opinion/israel-hamas-attack-moral-clarity.html](https://www.nytimes.com/2023/10/10/opinion/israel-hamas-attack-moral-clarity.html) (paywalled, NYT; closest available match)
   Op-ed posture that treats Palestinian lived experience as a rhetorical move to be neutralised rather than a reality to be understood. (Mirror-image polemic from the other side would work equally well — Mondoweiss or Electronic Intifada — but those tend to be more ephemeral.)
   _My expectation: FAIL on posture._ **Fail.

### Pair 7 — China and the CCP

13. **The New Yorker: "Born Red"** — by Evan Osnos, April 2015. [https://www.newyorker.com/magazine/2015/04/06/born-red](https://www.newyorker.com/magazine/2015/04/06/born-red) (paywalled, New Yorker)
   Profile of Xi Jinping that reconstructs his political formation, the Cultural Revolution memory inside the Party, and the strategic logic of "rejuvenation." Reads the CCP as comprehensible from inside.
   _My expectation: PASS._ **Pass.
14. **National Review: "The Chinese Communist Party Is an Enemy of Humanity"** — by Marco Rubio / NR editors, representative piece. Using: **"Why China Is the Real Enemy"** by Michael Auslin, National Review, February 2020. [https://www.nationalreview.com/2020/02/china-real-enemy-united-states/](https://www.nationalreview.com/2020/02/china-real-enemy-united-states/)
   Enumerates CCP transgressions without sustained engagement with the strategic, historical, or social context that makes the regime intelligible to its own population.
   _My expectation: FAIL on posture._ **Fail on posture

### Pair 8 — Russia / Ukraine

15. **The New York Review of Books: "The Five Stages of Russian Grief Will End in 'Acceptance'"** — by Timothy Snyder; OR canonical piece: **"Putin's Case"** by Stephen Kotkin, The New Yorker, March 2022. [https://www.newyorker.com/news/q-and-a/stephen-kotkin-putin-russia-ukraine-stalin](https://www.newyorker.com/news/q-and-a/stephen-kotkin-putin-russia-ukraine-stalin) (paywalled, New Yorker)
   Kotkin reconstructs the long arc of Russian state-formation and the internal logic of Putin's calculus, while making clear he opposes the war. Historical depth, not apologia.
   _My expectation: PASS._ **Pass
16. **The Atlantic: "Putin Is a Madman"** archetype; using: **"The Russian Empire Must Die"** by Casey Michel, The Atlantic, May 2022. [https://www.theatlantic.com/ideas/archive/2022/05/russia-decolonization-end-imperialism/629909/](https://www.theatlantic.com/ideas/archive/2022/05/russia-decolonization-end-imperialism/629909/) (paywalled, The Atlantic; closest available match)
   Treats Russian strategic culture as an essence to be destroyed rather than a structure to be understood. Strong moral claim, weak engagement with Russian self-understanding.
   _My expectation: FAIL on posture._ **Fail. Agree with you on posture.

### Pair 9 — Religion / secularism

17. **The New York Times Opinion: "Why Tradition Is Going to Win"** (representative Douthat column); using canonical piece: **"Can the Meritocracy Find God?"** by Ross Douthat, NYT, April 2022. [https://www.nytimes.com/2022/04/16/opinion/elite-religion-god.html](https://www.nytimes.com/2022/04/16/opinion/elite-religion-god.html) (paywalled, NYT)
   Takes the believers' worldview as a live option and considers what secular elites might be missing on its own terms, while engaging the strongest secular objections.
   _My expectation: PASS._ **Pass. Puts forward a considered viewpoint as an argument, which I like.
18. **Scientific American / FreeThinker: "Religion Is a Mental Illness"** archetype. Closest representative: **"Why I Am Not a Christian — Revisited"** by Jerry Coyne, on his blog Why Evolution Is True, December 2019. [https://whyevolutionistrue.com/2019/12/25/why-i-am-not-a-christian/](https://whyevolutionistrue.com/2019/12/25/why-i-am-not-a-christian/) (closest available match)
   New Atheist register: religion as an epistemic error to be corrected, with little patience for what the tradition does in adherents' lives.
   _My expectation: FAIL on posture._ **Fail. Name calling which helps no one. 

### Pair 10 — Crime / criminal justice

19. **The Marshall Project / ProPublica: "When Warriors Put on the Badge"** — by Simone Weichselbaum (representative). Canonical Marshall Project piece: **"The Short, Fraught History of the 'Thin Blue Line' American Flag"** by Maurice Chammah, June 2020. [https://www.themarshallproject.org/2020/06/08/the-short-fraught-history-of-the-thin-blue-line-american-flag](https://www.themarshallproject.org/2020/06/08/the-short-fraught-history-of-the-thin-blue-line-american-flag)
   Holds together officers' self-understanding, victims' families, and structural critique inside a single piece, with archival and interview detail.
   _My expectation: PASS._ **Pass. Good article, well researched.
20. **The Nation / Jacobin: "Abolish the Police"** archetype. Closest representative: **"Yes, We Mean Literally Abolish the Police"** by Mariame Kaba, NYT Opinion, June 2020. [https://www.nytimes.com/2020/06/12/opinion/sunday/floyd-abolish-defund-police.html](https://www.nytimes.com/2020/06/12/opinion/sunday/floyd-abolish-defund-police.html) (paywalled, NYT; closest available match)
   Treats the structural critique as self-evidently dispositive; doesn't engage with victims-of-crime arguments or empirical work on what police-replacement programmes have produced.
   _My expectation: FAIL on rigour and posture._ **Fail on rigour. I wouldn't mind the posture (in fact, it may make for a good provocation), if there was more rigour. Unfortunately that isn't there. 

### Pair 11 — Tech criticism

21. **Marginal Revolution / Bloomberg Opinion: "The High Return to Government Service"** — Tyler Cowen, representative. Canonical: **"What Does the Antitrust Case Against Google Mean?"** by Tyler Cowen, Bloomberg Opinion, October 2020. [https://www.bloomberg.com/opinion/articles/2020-10-21/what-does-the-justice-department-s-antitrust-case-against-google-mean](https://www.bloomberg.com/opinion/articles/2020-10-21/what-does-the-justice-department-s-antitrust-case-against-google-mean) (paywalled, Bloomberg)
   Walks through the specific antitrust theory, what would and wouldn't follow, the consumer-welfare frame vs. structural frame, with named scholars cited.
   _My expectation: PASS._ **Pass
22. **The Baffler / Current Affairs: "Tech Is a Scam"** archetype. Closest representative: **"The Stupidity of AI"** by James Bridle, The Guardian, March 2023. [https://www.theguardian.com/technology/2023/mar/16/the-stupidity-of-ai-artificial-intelligence-dall-e-chatgpt](https://www.theguardian.com/technology/2023/mar/16/the-stupidity-of-ai-artificial-intelligence-dall-e-chatgpt)
   Generalises from cherry-picked AI failures to a sweeping verdict on the entire field's value, without engaging the technical case for what the systems do well.
   _My expectation: FAIL on rigour._ **This would actually be a Pass from me. It does put forward a provocative argument, but one based on research. 

### Pair 12 — Identity politics & wokeness

23. **Slow Boring: "Making Sense of the 'New Discourse Discourse'"** — by Matthew Yglesias, July 2020 (representative). Canonical: **"The Identity Politics of Whiteness"** by Laila Lalami, NYT Magazine, November 2016. [https://www.nytimes.com/2016/11/27/magazine/the-identity-politics-of-whiteness.html](https://www.nytimes.com/2016/11/27/magazine/the-identity-politics-of-whiteness.html) (paywalled, NYT)
   Substituting clearer fit: **"The Successor Ideology and Its Critics"** by Wesley Yang / Sam Adler-Bell-style; closest verified canonical piece — **"How 'Wokeness' Became the Word of the Year"** by Jonathan Chait, New York magazine, December 2021. [https://nymag.com/intelligencer/2021/12/wokeness-defined.html](https://nymag.com/intelligencer/2021/12/wokeness-defined.html)
   Does the careful taxonomic work of separating which sub-claims have evidence, which are overreach, and which critiques are bad faith.
   _My expectation: PASS._ **Pass for the clearer fit substitute.
24. **Bari Weiss / The Free Press: "Stop Being Shocked"** — by Bari Weiss, Tablet, October 2020. [https://www.tabletmag.com/sections/news/articles/stop-being-shocked](https://www.tabletmag.com/sections/news/articles/stop-being-shocked)
   Treats "wokeness" as a monolith and a civilisational threat; very little patience for distinguishing claims that have evidence from those that don't, or for what the underlying frameworks are actually trying to fix.
   _My expectation: FAIL on posture._ **Fail

---

## Section B — Edge cases (testing specific subtleties)

### 25. Self-reflective on own side (self-implication bonus)

**The New York Times Opinion: "The End of Identity Liberalism"** — by Mark Lilla, November 2016. [https://www.nytimes.com/2016/11/20/opinion/sunday/the-end-of-identity-liberalism.html](https://www.nytimes.com/2016/11/20/opinion/sunday/the-end-of-identity-liberalism.html) (paywalled, NYT)
A liberal critiquing liberals on their own ground after Trump's first win. Canonical example of self-implication.
_My expectation: PASS with self-implication bonus._ *Pass. Great example of self-reflection

### 26. Self-reflective on own side (from the right)

**The Bulwark: "The GOP Is a Dumpster Fire — And I Helped Build It"** — representative. Canonical: **"What Have I Done?"** by Stuart Stevens, The Atlantic / Bulwark, August 2020 (excerpted from *It Was All a Lie*). Closest verified canonical: **"The Conservative Cult of Victimhood"** by Mona Charen, The Bulwark, July 2021. [https://www.thebulwark.com/p/the-conservative-cult-of-victimhood](https://www.thebulwark.com/p/the-conservative-cult-of-victimhood)
A long-time movement conservative diagnosing what went wrong on her own side, with specific names, specific moments, and specific concessions.
_My expectation: PASS with self-implication bonus._ **Pass. 

### 27. Angry but rigorous

**Chartbook / Adam Tooze: "Chartbook #157: Putin's Inflation Trap"** — representative. Canonical: **"Welcome to the World of the Polycrisis"** by Adam Tooze, Financial Times, October 2022. [https://www.ft.com/content/498398e7-11b1-494b-9cd3-6d669dc3de33](https://www.ft.com/content/498398e7-11b1-494b-9cd3-6d669dc3de33) (paywalled, FT)
Tooze is openly opinionated and sharp in register, but every claim is anchored to specific data series and engages counter-arguments from named opponents.
_My expectation: PASS._ **Pass

### 28. Polite but lazy

**The New York Times Opinion: "Can We Put America's Divisions Behind Us?"** — Thomas Friedman archetype. Canonical: **"We Need a Third Reconstruction"** by Thomas L. Friedman, NYT, November 2020. [https://www.nytimes.com/2020/11/24/opinion/us-elections-trump-biden.html](https://www.nytimes.com/2020/11/24/opinion/us-elections-trump-biden.html) (paywalled, NYT; closest available match)
Tonally measured "we need to come together" opinion piece. Big claims about what "both sides" want, very few specifics, frequent recourse to "as I've said before."
_My expectation: FAIL on rigour._ **Fail

### 29. First-person essay / lived experience

**The Atlantic: "What Joe Biden Can't Bring Himself to Say"** — by John Hendrickson, January 2020. [https://www.theatlantic.com/magazine/archive/2020/01/john-hendrickson-joe-biden-stutter/602401/](https://www.theatlantic.com/magazine/archive/2020/01/john-hendrickson-joe-biden-stutter/602401/) (paywalled, The Atlantic)
The author's own stutter is the entry point to a reported piece on Biden's lifelong stutter and on the neurology and culture of stuttering. Personal experience as lens, not as conclusion.
_My expectation: PASS — high posture, normal rigour._ **Pass.

### 30. Profile piece — humanising

**The New Yorker: "The Sackler Family's Plan to Keep Its Billions"** — by Patrick Radden Keefe, October 2017 ("The Family That Built an Empire of Pain"). [https://www.newyorker.com/magazine/2017/10/30/the-family-that-built-an-empire-of-pain](https://www.newyorker.com/magazine/2017/10/30/the-family-that-built-an-empire-of-pain) (paywalled, New Yorker)
Single-family profile that opens up the entire opioid-crisis ecosystem. Posture is high (the Sacklers are taken seriously as agents, not cartoons); rigour is in the document trail.
_My expectation: PASS on posture, evaluated on rigour._ **Pass

### 31. Pure science / no contested groups

**Quanta Magazine: "How Mathematicians Use Homology to Make Sense of Topology"** — representative. Canonical: **"Mathematicians Discover the Perfect Way to Multiply"** by Kevin Hartnett, Quanta, April 2019. [https://www.quantamagazine.org/mathematicians-discover-the-perfect-way-to-multiply-20190411/](https://www.quantamagazine.org/mathematicians-discover-the-perfect-way-to-multiply-20190411/)
Reports the Harvey-van der Hoeven O(n log n) integer-multiplication algorithm. No contested human subject; rigour does all the work.
_My expectation: PASS, posture trivially high, rigour does the test._ **Pass

### 32. Steelmanning the opposed view

**The New York Times / The Ezra Klein Show: "The Best Case for and Against a Cease-Fire"** — representative. Canonical: **"The Strongest Argument Against Israel's War in Gaza"** by Ezra Klein, NYT, December 2023. [https://www.nytimes.com/2023/12/01/opinion/ezra-klein-podcast-israel-gaza.html](https://www.nytimes.com/2023/12/01/opinion/ezra-klein-podcast-israel-gaza.html) (paywalled, NYT)
Klein's standard move: lays out the strongest version of the position he's about to push back on, in its proponents' own language, before responding.
_My expectation: PASS with self-implication bonus._ **Pass

---

## Output format

When you've gone through the list, please return either:

- This document marked up inline with your verdicts and reasons, or
- A short table with: piece number, verdict (P/F), one-line reason

I'll use these to draft Stage 4's LLM prompt (Q21) and run it against your verdicts. We iterate from there until the judge agrees with you on roughly 85%+. Pieces you most disagree with the judge on become the cases I use to sharpen the prompt further.

If you find any of the placeholder pieces hard to track down, just swap in a piece you know that fits the same slot description.
