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

### Chart Container

- **Structure**: title/control cluster and responsive visualization.
- **States**: populated, loading, empty, and error.
- **Layout**: full-width fluid region contained by the page gutter.

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
