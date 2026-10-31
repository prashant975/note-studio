# QBG Studio design system

One stylesheet (`static/style.css`), one icon macro (`templates/_icons.html`),
no build step, no CDN, no icon font. Everything below already exists — build
screens by composing these, not by writing new CSS or inline styles.

**The rule:** if a screen needs a look that is not here, add a component to
`style.css` and document it here. Avoid `style="..."` except for a computed
width (a progress bar's `width: N%`); a margin or alignment need is met by
the utilities under *Added after verification*, not by an inline override.

---

## Tokens

Never hard-code a colour, size or space. The tokens are the only vocabulary.

| Group | Tokens |
|---|---|
| Surface | `--c-canvas` `--c-surface` `--c-surface-2` `--c-surface-3` `--c-hover` |
| Border | `--c-border` `--c-border-soft` `--c-border-hard` |
| Text | `--c-text` `--c-text-2` `--c-text-3` |
| Brand | `--c-brand` `--c-brand-hover` `--c-brand-soft` `--c-brand-line` `--c-brand-text` |
| Semantic | `--c-ok` `--c-warn` `--c-danger` (+ `-soft` and `-line` for each) |
| Type | `--t-11 --t-12 --t-13 --t-14 --t-16 --t-20 --t-24 --t-30` |
| Space | `--s-1 --s-2 --s-3 --s-4 --s-5 --s-6 --s-8 --s-10 --s-12 --s-16` (4px grid) |
| Shape | `--r-xs --r-sm --r --r-lg --r-full` |
| Depth | `--sh-1` `--sh-2` `--sh-pop`, `--ring` for focus |

Dark mode is automatic — every token is redefined under
`prefers-color-scheme: dark`. Never write a raw hex in a template.

---

## Icons

```jinja
{% import "_icons.html" as ico with context %}
{{ ico.i('play') }}              {# 16px, inherits colour #}
{{ ico.i('alert-triangle', 18) }}
{{ ico.state('warn', 18) }}      {# the icon matching a semantic state #}
```

Available: `gauge layers plus check-circle languages sparkles send settings
menu x play stop pause refresh alert-triangle alert-circle check info clock
database browser key file-text download external chevron-right arrow-right
search help eye copy shield inbox list-checks book`.

An icon **never** carries meaning alone — it sits beside a word, or the
control gets an `aria-label`.

---

## Layout

`base.html` provides the shell. A page only fills blocks:

```jinja
{% extends "base.html" %}
{% block title %}Thing - QBG Studio{% endblock %}
{% block crumb %}<a href="/x">Area</a><span class="sl">/</span><span class="now">Thing</span>{% endblock %}
{% block topacts %}<a class="btn sm" href="/y">Secondary</a>{% endblock %}
{% block wrapclass %}wide{% endblock %}   {# or `narrow`; default is 1180px #}
{% block body %} ... {% endblock %}
```

### Page header

```html
<div class="page-head">
  <div class="grow">
    <h1>Translation Bot</h1>
    <p class="sub">One sentence saying what this screen is for.</p>
    <div class="meta">
      <span class="chip ok">Ready</span>
      <span class="small muted">Last run 4 minutes ago</span>
    </div>
  </div>
  <div class="acts"><a class="btn primary" href="#">Primary action</a></div>
</div>
```

---

## Card

The main container. Padding lives on the parts, so tables can sit flush.

```html
<div class="card">
  <div class="hd">
    <div class="grow">
      <h2>Section</h2>
      <p class="d">Optional one-line description.</p>
    </div>
    <div class="acts"><button class="btn sm">Action</button></div>
  </div>
  <div class="bd"> ... </div>
  <div class="ft"> ... optional, tinted footer ... </div>
</div>
```

Variants: `.card.tight` (smaller body padding), `.card.flush` (no body
padding, for a full-bleed table), `.card.plain` (no shadow).

Grids: `.grid.cols-2`, `.grid.cols-3`.

---

## Buttons

```html
<button class="btn primary">{{ ico.i('play', 14) }} Run</button>
<button class="btn">Secondary</button>
<button class="btn ghost">Tertiary</button>
<button class="btn danger">Stop</button>
<button class="btn icon ghost" aria-label="Close">{{ ico.i('x') }}</button>
```

Sizes `.sm` / default / `.lg`. Wrap groups in `.btn-row`.

Rules:
- **One `.primary` per row.** The CSS demotes a second one on purpose.
- A disabled button **must** carry a `title` saying why, and the page should
  state the reason in a `.note` near it.
- Never render an important action as a bare link.
- `data-confirm="..."` on a `<form>` gets a confirmation naming the count.
- Submitting a form sets `aria-busy` on its button automatically.

---

## Status and badges

```html
<span class="chip ok">Connected</span>      <!-- also warn / danger / info -->
<span class="chip">Neutral</span>
```

State is **never** colour alone — pair the chip with an icon or a word.

### Connection cards

```jinja
{% include "_health.html" %}                         {# full, with fixes #}
{% with compact = True %}{% include "_health.html" %}{% endwith %}
```

Driven by `pb.health()`. Do not hand-roll a status list — five screens use
this one component so they cannot disagree.

### Metrics

```html
<div class="counters">
  <div class="counter brand"><div class="n">18</div><div class="l">waiting</div></div>
  <div class="counter danger"><div class="n">3</div><div class="l">need review</div></div>
</div>
```

Modifiers: `ok` `warn` `danger` `brand`.

---

## Alerts

```html
<div class="note warn">
  <span class="nico">{{ ico.i('alert-triangle', 18) }}</span>
  <div class="ntext">
    <div class="hd">What happened</div>
    <div class="bd">What to do about it.</div>
    <div class="btn-row"><a class="btn sm" href="#">Fix it</a></div>
  </div>
</div>
```

Kinds: `ok` `warn` `danger` `info`, or none for neutral.

Every error must answer: what failed, why, what to do next. Machine text
(tracebacks, paths, HTTP codes) goes inside `<details class="raw">`.

---

## Progress

```html
<div class="bar"><i style="width: 40%"></i></div>   <!-- .ok .warn .danger .idle -->
<div class="progress-line">
  <span class="spinner"></span>
  <span class="msg">Working on row 12...</span>
</div>
<div class="progress-facts">
  <span>Row 12 of 48</span><span class="bad">3 errors</span><span>about 14 min left</span>
</div>
```

`.bar.idle` is the indeterminate form — use it when there is no honest
denominator rather than inventing one. `{{ job_eta(job) }}` returns an
estimate or an empty string; it needs at least two finished rows.

---

## Tables

```html
<div class="tablewrap">
  <table class="data">
    <thead><tr><th>Question</th><th>Status</th><th class="acts"></th></tr></thead>
    <tbody>
      <tr>
        <td>MS-01_Q003</td>
        <td><span class="chip ok">Uploaded</span></td>
        <td class="acts"><a class="btn sm" href="#">View</a></td>
      </tr>
    </tbody>
  </table>
</div>
```

`.tablewrap` gives horizontal scroll on small screens — always wrap a table
in it. `th` is `position: sticky`, but because `.tablewrap` is a scroll
container the header only sticks when the `.tablewrap` itself has a bounded
height (a `max-height` with `overflow: auto`); inside an unbounded wrapper it
scrolls away with the page. `td.num` right-aligns numbers, `td.wrap-cell`
lets long text wrap instead of widening the table.

---

## Empty state

```html
<div class="empty">
  <div class="eico">{{ ico.i('inbox', 20) }}</div>
  <div class="big">Nothing queued yet</div>
  <p>Say what the reader should do, not just that there is no data.</p>
  <div class="btn-row" style="justify-content:center">
    <a class="btn primary" href="/queue">Send questions</a>
  </div>
</div>
```

---

## Progressive disclosure

Explanation a first-time reader wants once, and a daily user never does:

```html
<details class="helpbox">
  <summary>How this works</summary>
  <div class="bd"> ... </div>
</details>
```

Screens lead with state and controls. Long paragraphs go in here.

---

## Toasts

```js
QBG.toast("9 questions translated", "ok")   // ok | warn | danger | info
```

From the server, redirect with `?toast=Message&kind=ok` — `app.js` shows it
and strips it from the address bar so a refresh does not replay it.

Use for something that happened **off-screen**. Never for state already
visible on the page.

---

## Copy

- Say the fact, not the mechanism: "18 questions detected", not
  "18 row(s), both status columns present."
- Numbers in confirmations: "Create 40 questions on the live portal?"
- Never show a path, a traceback or an HTTP code in the main flow.
- Disabled means explained.

---

## Accessibility

- Every icon-only control has `aria-label`.
- Focus is `:focus-visible` with a visible ring; never remove it.
- State is icon + word, not colour.
- Tables have real `<th>`; live regions use `aria-live="polite"`.
- The shell has a skip link and the drawer closes on `Escape`.

---

## Added after verification

These exist because a verifier found a template solving the need inline.

| Need | Use |
|---|---|
| Small spinner inside a chip | `.spinner.sm` |
| Nudge one component | `.mt-2 .mt-3 .mt-4 .mb-0 .mb-2 .mb-3 .mb-4`, or `.flow` on a parent for even rhythm |
| Alert with no bottom gap (last in a card) | `.note.flush` |
| Keep server line breaks | `.pre` |
| Right-align a table header over `.num` / `.acts` cells | `th.num`, `th.acts` |
| Muted text inside a table | `td.muted` or `tr.muted` — plain `.muted` loses to the cell colour |
| Fixed table column widths | `th.w-xs` (40) `.w-sm` (96) `.w-md` (160) `.w-lg` (240) |
| A counter whose value is a word | `<div class="n word">Idle</div>` |
| Short inline input | `<label class="field narrow">` / `.medium` |
| Clipped question text with a fade | `<div class="qbody clip">` |
| Numbered recovery steps | `<ol class="steps">` |
| Success button | `.btn.ok` (has a hover now) |
| Machine text inside an alert | `<details class="raw">` nests inside `.note` |
| HTMX-shaped refusal | `_blocked_note.html` with `why`, `href`, `label` |

Progress bars: add `role="progressbar" aria-valuenow="N" aria-valuemin="0"
aria-valuemax="100"` and put the number in text beside the bar.

Pollers and swap targets: `aria-live="polite"` on the container.

Decorative spinners inside chips: `aria-hidden="true"`.
