import Link from '@docusaurus/Link';
import useBaseUrl from '@docusaurus/useBaseUrl';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import type { ReactNode } from 'react';

import styles from './index.module.css';

const FEATURES = [
  {
    title: 'Tabbed consoles',
    body: 'Open several guests at once, or span full screen across every monitor.',
    to: '/docs/using/consoles',
    link: 'Consoles',
  },
  {
    title: 'Local hardware in the guest',
    body: 'Passthrough a USB device, a microphone, or a smartcard reader to a VM over SPICE.',
    to: '/docs/sharing/usb-redirection',
    link: 'Sharing hardware and files',
  },
  {
    title: 'Folder tree',
    body: 'Organize guests into a custom folder tree, in addition to the default and tagged views.',
    to: '/docs/using/inventory',
    link: 'The inventory tree',
  },
  {
    title: 'Node view',
    body: 'View meters and history graphs for each of your nodes, and easily access a root shell.',
    to: '/docs/using/nodes-and-tasks',
    link: 'Nodes and tasks',
  },
];

function Hero(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className={styles.hero}>
      <div className={`container ${styles.heroInner}`}>
        <img className={styles.mark} src={useBaseUrl('/img/proxima.png')} alt="" />
        <h1 className={styles.title}>{siteConfig.title}</h1>
        <p className={styles.tagline}>
          A desktop client for Proxmox VE
        </p>
        <div className={styles.buttons}>
          <Link className={styles.primaryButton} to="/docs/">
            Documentation
          </Link>
          <Link
            className={styles.secondaryButton}
            to="https://github.com/andyjsmith/proxima/releases/latest">
            Download
          </Link>
        </div>
        <p className={styles.platforms}>
          Windows, Linux, and macOS
        </p>
      </div>
    </header>
  );
}

export default function Home(): ReactNode {
  return (
    <Layout
      title="A desktop client for Proxmox VE"
      description="Proxima is a desktop client for Proxmox VE: guests in tabs, SPICE and VNC consoles, USB and folder sharing, and certificate pinning.">
      <Hero />

      <div className={styles.shotWrap}>
          <img src={useBaseUrl('/img/screenshots/main_window.png')} alt="Proxima screenshot" />
      </div>

      <main>
        <section className={`container ${styles.section}`}>
          <h2 className={styles.sectionTitle}>Features</h2>
          <div className={styles.grid}>
            {FEATURES.map((feature) => (
              <div className={styles.card} key={feature.title}>
                <div className={styles.cardTitle}>{feature.title}</div>
                <p>{feature.body}</p>
                <p style={{marginTop: '0.7rem'}}>
                  <Link to={feature.to}>{feature.link}</Link>
                </p>
              </div>
            ))}
          </div>
        </section>
      </main>
    </Layout>
  );
}
