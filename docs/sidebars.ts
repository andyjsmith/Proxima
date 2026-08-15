import type { SidebarsConfig } from '@docusaurus/plugin-content-docs';

/**
 * Two sidebars, and the split is deliberate. Everything a user needs is in
 * the first; building, testing and releasing Proxima is in the second, which
 * is a single page reached from its own navbar entry.
 */
const sidebars: SidebarsConfig = {
  docs: [
    'intro',
    {
      type: 'category',
      label: 'Getting started',
      collapsed: false,
      items: ['getting-started/installation', 'getting-started/connecting'],
    },
    {
      type: 'category',
      label: 'Using Proxima',
      collapsed: false,
      items: [
        'using/inventory',
        'using/consoles',
        'using/display-adapters',
        'using/status-bar',
        'using/guests',
        'using/nodes-and-tasks',
      ],
    },
    {
      type: 'category',
      label: 'Sharing hardware and files',
      collapsed: false,
      items: [
        'sharing/file-drag-and-drop',
        'sharing/usb-redirection',
        'sharing/audio',
        'sharing/smartcard',
      ],
    },
    {
      type: 'category',
      label: 'Configuration',
      collapsed: false,
      items: [
        'configuration/preferences',
        'configuration/per-guest-settings',
        'configuration/settings-file',
        'configuration/performance',
      ],
    },
    'troubleshooting',
  ],

  developer: ['development'],
};

export default sidebars;
