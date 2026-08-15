import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'Proxima',
  tagline: 'A desktop client for Proxmox VE',
  favicon: 'img/proxima.png',

  future: {
    v4: true,
  },

  // GitHub Pages project site: https://andyjsmith.github.io/proxima/. For a
  // custom domain, put a CNAME file in static/, set url to the domain and
  // baseUrl back to '/'.
  url: 'https://andyjsmith.github.io',
  baseUrl: '/proxima/',

  organizationName: 'andyjsmith',
  projectName: 'proxima',
  // Stated rather than left undefined, because GitHub Pages adds a trailing
  // slash of its own. With this on, the links the site generates are the
  // URLs Pages serves, so nothing takes a redirect on the way.
  trailingSlash: true,

  onBrokenLinks: 'throw',

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          editUrl: 'https://github.com/andyjsmith/proxima/tree/main/docs/',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  // Search runs entirely in the browser against an index built at build time,
  // so there is no service to sign up for and nothing leaves the reader's
  // machine. It is a theme rather than a plugin because it replaces the
  // navbar's search box.
  themes: [
    [
      '@easyops-cn/docusaurus-search-local',
      {
        hashed: true,
        indexDocs: true,
        indexPages: false,
        indexBlog: false,
        docsRouteBasePath: '/docs',
        // Match whole words as prefixes, so "adapt" finds "adapters".
        removeDefaultStopWordFilter: false,
        highlightSearchTermsOnTargetPage: true,
        searchResultLimits: 8,
        searchResultContextMaxLength: 60,
        explicitSearchResultPath: true,
      },
    ],
  ],

  themeConfig: {
    colorMode: {
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'Proxima',
      logo: {
        alt: 'Proxima',
        src: 'img/proxima.png',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docs',
          position: 'left',
          label: 'Documentation',
        },
        {
          type: 'docSidebar',
          sidebarId: 'developer',
          position: 'left',
          label: 'Developer',
        },
        {
          href: 'https://github.com/andyjsmith/proxima/releases/latest',
          label: 'Download',
          position: 'right',
        },
        {
          href: 'https://github.com/andyjsmith/proxima',
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Documentation',
          items: [
            {label: 'Overview', to: '/docs/'},
            {label: 'Installation', to: '/docs/getting-started/installation'},
            {label: 'Troubleshooting', to: '/docs/troubleshooting'},
          ],
        },
        {
          title: 'Project',
          items: [
            {
              label: 'Releases',
              href: 'https://github.com/andyjsmith/proxima/releases',
            },
            {
              label: 'Issues',
              href: 'https://github.com/andyjsmith/proxima/issues',
            },
            {
              label: 'GitHub',
              href: 'https://github.com/andyjsmith/proxima',
            },
          ],
        },
      ],
      copyright: `Proxima is MIT licensed. Documentation built with Docusaurus.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['bash', 'ini', 'json', 'powershell'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
