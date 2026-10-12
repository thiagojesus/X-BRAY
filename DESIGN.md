# X-BRAY Design System

## 1. Atmosphere & Identity

X-BRAY is a compact, dark macroeconomic command center. Near-black navigation, blue-black data surfaces, restrained borders, and a teal interaction accent create a dense but legible analytical workspace; the persistent sidebar is its signature frame.

## 2. Color

| Role | Token | Value | Usage |
|---|---|---|---|
| Background / primary | `--bg-primary` | `#0a0a1a` | Page canvas |
| Background / secondary | `--bg-secondary` | `#12122a` | Header and raised page regions |
| Background / card | `--bg-card` | `#1a1a2e` | Cards, charts, and information panels |
| Background / sidebar | `--sidebar-bg` | `#07070f` | Persistent navigation |
| Border | `--border` | `#2a2a4a` | Dividers and component outlines |
| Text / primary | `--text-primary` | `#eee` | Headings and primary values |
| Text / secondary | `--text-secondary` | `#999` | Labels, metadata, and supporting copy |
| Accent | `--accent` | `#4ecdc4` | Active navigation and interactive emphasis |

Chart series use the existing categorical palette owned by each chart module. New shell and component colors must use the semantic tokens above.

## 3. Typography

The UI uses the native system stack: `-apple-system`, `BlinkMacSystemFont`, `Segoe UI`, `Roboto`, `sans-serif`.

| Level | Size | Weight | Usage |
|---|---|---|---|
| Value | `1.4rem` | 700 | KPI values |
| Page heading | `1.1rem` | 600 | Current route title |
| Component heading | `0.9rem` to `0.95rem` | 600 | Charts and cards |
| Body / control | `0.78rem` to `0.85rem` | 400 to 600 | Navigation, controls, and body copy |
| Caption | `0.7rem` to `0.75rem` | 400 to 500 | Dates, labels, and metadata |

Mobile variants may use the existing smaller values already defined in `index.css`; no new type tier is introduced for responsive shell fixes.

## 4. Spacing & Layout

Spacing follows the existing quarter-rem rhythm, primarily `0.25rem`, `0.5rem`, `0.75rem`, `1rem`, `1.25rem`, `1.5rem`, and `2rem`. The desktop shell reserves `--sidebar-width` (`240px`) for a fixed sidebar and lets the main region fill the remaining inline space.

- At `1024px` and below, the sidebar becomes a fixed off-canvas drawer and the main margin resets to zero.
- At `640px` and below, page gutters reduce to `1rem` and grids reflow.
- The document owns vertical scrolling. The sidebar independently owns navigation overflow.
- Every fluid flex or grid child must be allowed to shrink within the viewport; primary content must not create document-level horizontal scrolling.

## 5. Components

### Application Shell

- **Structure**: fixed `aside`, backdrop, and fluid `main` containing a sticky header and page content.
- **Variants**: desktop sidebar; mobile/tablet off-canvas drawer.
- **States**: drawer closed and drawer open.
- **Accessibility**: the header exposes a named native button that controls drawer state; the open drawer exposes its own visible native close button and does not rely solely on backdrop dismissal.
- **Motion**: drawer movement uses `transform`; backdrop visibility uses `opacity`.
- **Layout**: fixed-sidenav shell with document scroll and shrinkable main content.

### Navigation Item

- **Structure**: Lucide icon plus text label.
- **States**: default, hover, and active route.
- **Accessibility**: route items remain native links; drawer dismissal remains a native button styled with the same row anatomy.

### Data Card

- **Structure**: label, value, and date or supporting text.
- **States**: default and hover lift.
- **Layout**: responsive intrinsic grid using the shared card treatment.

### Derived Indicator Card

- **Structure**: indicator title and state, neutral interpretation note, value, base date, calculation cutoff, methodology version, and a native details disclosure for formula and persisted input provenance.
- **States**: available, persisted unavailable, missing snapshot, malformed payload, loading, request error, and stale cached response; each indicator keeps its state independent from its siblings.
- **Layout**: three columns on wide screens, two columns below `900px`, and one column below `560px`; cards align to the start so an expanded disclosure does not stretch its siblings. Content may shrink and wrap without document-level horizontal overflow.
- **Accessibility**: each article is named by its heading; methodology uses keyboard-operable native `details` and `summary`.

### Macro Score Overview

- **Structure**: persisted score and classification, Beta version, score month, information cutoff, and overall coverage.
- **States**: loading, full coverage, partial coverage, score unavailable, stale cached snapshot, transport error, and no snapshot; unavailable scores never use a numeric placeholder.
- **Accessibility**: coverage and status always include text and numeric values; methodology and provenance remain reachable without hover.

### Macro Score Dimension Card

- **Structure**: dimension label, persisted score, valid/total component counts, coverage, and a native disclosure of component details.
- **States**: available, partial, and unavailable dimensions retain their API-provided reasons.
- **Layout**: three columns on wide screens, two columns below `900px`, and one column below `560px`; expanded cards align to the start and shrink without document-level horizontal overflow.
- **Accessibility**: the card is named by its heading; each component has a second keyboard-operable disclosure for its persisted input provenance, keeping source details within two activations.

### Macro Score History

- **Structure**: persisted overall or dimension scores over score months, with a visible series title, unit, explanatory summary, and accessible value table.
- **States**: loading, populated, empty history, and request error are distinct; missing months and null scores remain gaps and are never interpolated.
- **Layout**: full-width fluid chart region contained by the page gutter; the series selector wraps on narrow screens.
- **Accessibility**: series selection uses a named native control; chart meaning and values remain available without color or hover.
- **Value representation**: retain the native value table above `560px`; at `560px` and below, show a stacked semantic list with visible month, score, state, and information-cutoff labels for each entry.
- **Shared content**: both representations use the same 120-month persisted history and selected series, preserving score months, scores, and score cutoffs. Explicitly distinguish a missing snapshot from a persisted null/unavailable score; neither becomes zero or an interpolated value.
- **Responsive layout**: stacked entries use the available inline width without a horizontal inner scroller. Cutoffs and state text wrap within their entries at `375px` and `200%` zoom without clipping or narrow table-column fragmentation.
- **Responsive accessibility**: hide the inactive table or list from both layout and the accessibility tree at the breakpoint; expose only one value representation, with native table semantics on desktop and native list semantics plus explicit field labels on mobile.

### Macro Score Methodology Disclosure

- **Structure**: native `details`/`summary` describing methodology version `0.1.0-beta`, normalization, coverage thresholds, classification bands, and non-causal interpretation.
- **States**: available in every page state, including loading, empty, and transport error.
- **Accessibility**: keyboard-operable native disclosure; opening and closing preserves focus on the summary.

### Chart Container

- **Structure**: title/control cluster and responsive visualization.
- **States**: populated, loading, empty, and error.
- **Layout**: full-width fluid region contained by the page gutter.

### Metric Mode Selector

- **Structure**: compact grouped native buttons followed by the active unit label.
- **States**: default, hover, keyboard focus, and pressed; pressed state uses the shared teal accent treatment.
- **Accessibility**: the group has a descriptive name and every button exposes `aria-pressed`.
- **Layout**: wraps within the page gutter on narrow viewports and never forces horizontal document scrolling.

## 6. Motion & Interaction

Interactive color and transform feedback uses the established `150ms` to `200ms` timings. Drawer and backdrop transitions use `250ms ease`. Motion communicates hover, active state, loading, or drawer visibility; new decorative animation is not permitted.

## 7. Depth & Surface

X-BRAY uses mixed tonal separation and borders. Cards and panels use `--bg-card` with a one-pixel `--border` outline; the sticky header uses `--bg-secondary`; the mobile drawer alone receives a pronounced directional shadow to communicate overlay depth.

## 8. Accessibility Constraints & Accepted Debt

### Constraints

- Preserve native button and link semantics and visible browser focus treatment.
- Interactive icons require accessible names; visible text is preferred inside an open navigation drawer.
- Drawer dismissal must be operable without pointer backdrop interaction.
- Primary content must reflow without horizontal document scrolling at `375px`.

### Accepted Debt

| Item | Location | Why accepted | Owner / Exit |
|---|---|---|---|
| Legacy chart modules contain raw categorical and SVG colors. | `frontend/src/pages`, `frontend/src/charts` | Existing visualization behavior is outside this responsive shell fix. | Consolidate only during a dedicated chart-token task. |
| Several pre-existing controls use compact targets and rely on browser-default focus rings. | `frontend/src/index.css` | Changing global control sizing or focus styling would alter the established interface beyond this two-defect repair. | Address in a dedicated accessibility pass. |
