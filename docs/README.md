# Proxima documentation

The documentation site, built with [Docusaurus](https://docusaurus.io/).

```bash
npm install
npm start          # dev server on http://localhost:3000/proxima/
npm run build      # static site into build/
npm run serve      # serve what was built, also under /proxima/
```

The site is built for GitHub Pages at `https://andyjsmith.github.io/proxima/`,
so `baseUrl` is `/proxima/` and every link is written with that prefix. Preview
with `npm start` or `npm run serve`, both of which serve under `/proxima/`.
Opening `build/index.html` from the filesystem, or serving `build/` at a
server root, breaks every link. Change `url` and `baseUrl` in
`docusaurus.config.ts` if the site moves.

## Deployment

Two workflows, both scoped to `docs/`:

|                                          |                                                                                  |
| ---------------------------------------- | -------------------------------------------------------------------------------- |
| `.github/workflows/deploy-docs.yml`      | Push to `main`, or run by hand. Builds, then publishes to GitHub Pages.          |
| `.github/workflows/test-deploy-docs.yml` | Pull requests. Builds only, so a broken link fails the check without publishing. |

Enable publishing once, under **Settings > Pages > Source > GitHub Actions**.
Nothing else is needed: the artifact goes straight to Pages, so there is no
`gh-pages` branch and no deploy token.

`onBrokenLinks` is `throw`, so any dead internal link fails the build in CI
rather than shipping.

The landing page is `src/pages/index.tsx` with `src/pages/index.module.css`
beside it, and it is served at `/`. Documentation lives in `docs/` and is
served under `/docs/`, where `docs/intro.mdx` is the landing page. The sidebar
is defined in `sidebars.ts`, not generated from the filesystem, so a new page
needs an entry there.

Site wide styling is `src/css/custom.css`. The palette comes from the
application icon: orange `#ff7a18` and near black `#17171a`.

## Search

Search is local, through `@easyops-cn/docusaurus-search-local`. The index is
built into `build/search-index.json` at build time and queried in the browser,
so there is no service to sign up for and nothing leaves the reader's machine.

It indexes the documentation only, not the landing page. `npm start` does not
build an index, so search returns nothing in the dev server. Use
`npm run build && npm run serve` to test it.
