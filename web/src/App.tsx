/**
 * Load a `.cytos` slide and hand it to the viewer.
 *
 * Two ways in, one viewer. In a browser tab the slide is a URL, named with
 * `?slide=<url>`, e.g.
 *   http://localhost:5173/?slide=http://127.0.0.1:8787/pancreas_ffpe.cytos
 * (that default fills in when the param is missing). In the desktop shell it
 * is a directory on disk, chosen from File ▸ Open Slide… or named on the
 * command line, and there is no default — an empty window says so, the way
 * the Qt viewer's welcome window does.
 *
 * Wiring only: find the slide, load it, hold the settings. What loading means
 * is `io/slide.ts`; what the settings mean is `core/session.ts`; where the
 * bytes come from is `io/read.ts`.
 */

import { useEffect, useRef, useState } from 'react';

import { defaultSettings, pointsKey, segmentsKey, type SlideSettings } from './core/session';
import { loadFeatures, type FeatureTable } from './io/features';
import { loadGenes, type GeneTable } from './io/points';
import { desktopHost } from './io/host';
import { readerFor } from './io/read';
import { loadSlide, type LoadedSlide } from './io/slide';
import { SlideViewer } from './render/scene';
import type { CustomColors } from './ui/color-picker';
import { Panel } from './ui/panel';

const DEFAULT_SLIDE = 'http://127.0.0.1:8787/pancreas_ffpe.cytos';

export default function App() {
  const host = desktopHost();
  const params = new URLSearchParams(window.location.search);
  // ?view=x,y,zoom (full-res pixel coords) — start somewhere specific.
  const viewParam = params.get('view')?.split(',').map(Number);
  const initialView =
    viewParam?.length === 3 && viewParam.every(Number.isFinite)
      ? (viewParam as [number, number, number])
      : undefined;

  const [slideUrl, setSlideUrl] = useState<string | null>(() => {
    const named = params.get('slide')?.replace(/\/+$/, '');
    if (named) return named;
    return host ? null : DEFAULT_SLIDE;
  });
  const [slide, setSlide] = useState<LoadedSlide | null>(null);
  const [settings, setSettings] = useState<SlideSettings | null>(null);
  const [features, setFeatures] = useState<Record<string, FeatureTable | null>>({});
  const [genes, setGenes] = useState<Record<string, GeneTable | null>>({});
  const [error, setError] = useState<string | null>(null);
  // One pool of user-created colors for the whole window, shared by every
  // picker — a color mixed while tuning one layer is a one-click swatch on
  // the next. The Qt viewer saves this in the session (`custom_colors`);
  // the web viewer has no session file yet, so it lasts the page.
  const [customColors, setCustomColors] = useState<string[]>([]);
  const custom: CustomColors = {
    colors: customColors,
    add: (hex) =>
      setCustomColors((prev) => (prev.includes(hex) ? prev : [...prev, hex])),
    remove: (hex) => setCustomColors((prev) => prev.filter((c) => c !== hex)),
  };

  // The menu acts on whatever slide is loaded *now*, so it reads a ref rather
  // than closing over one — the listeners are registered once, not per slide.
  const slideRef = useRef<LoadedSlide | null>(null);
  slideRef.current = slide;

  useEffect(() => {
    if (!host) return;
    host.initialSlide().then((dir) => {
      if (dir) setSlideUrl(dir);
    });
    host.onOpenSlide(setSlideUrl);
    host.onResetSettings(() => {
      const current = slideRef.current;
      if (current) setSettings(defaultSettings(current.manifest));
    });
  }, [host]);

  useEffect(() => {
    if (!slideUrl) return;
    let cancelled = false;
    setSlide(null);
    setSettings(null);
    setFeatures({});
    setGenes({});
    setError(null);
    loadSlide(readerFor(slideUrl))
      .then((loaded) => {
        if (cancelled) return;
        setSlide(loaded);
        setSettings(defaultSettings(loaded.manifest));
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [slideUrl]);

  // Per-cell feature tables load after the first render, one fetch per
  // segment layer — the viewer recolors as each arrives.
  useEffect(() => {
    if (!slide || !slideUrl) return;
    let cancelled = false;
    const read = readerFor(slideUrl);
    for (const source of slide.segments) {
      const key = segmentsKey(source.spec.id);
      loadFeatures(read, source.spec.path)
        .then((table) => {
          if (!cancelled) setFeatures((prev) => ({ ...prev, [key]: table }));
        })
        .catch((err) => console.error(`features for ${key}:`, err));
    }
    return () => {
      cancelled = true;
    };
  }, [slide, slideUrl]);

  // Gene tables, one per point layer — names and whole-slide counts, so the
  // gene picker can list and rank without reading a single transcript.
  useEffect(() => {
    if (!slide || !slideUrl) return;
    let cancelled = false;
    const read = readerFor(slideUrl);
    for (const source of slide.points) {
      const key = pointsKey(source.spec.id);
      loadGenes(read, source.spec.path)
        .then((table) => {
          if (!cancelled) setGenes((prev) => ({ ...prev, [key]: table }));
        })
        .catch((err) => console.error(`genes for ${key}:`, err));
    }
    return () => {
      cancelled = true;
    };
  }, [slide, slideUrl]);

  if (!slideUrl) {
    return (
      <div style={{ padding: 24 }}>
        <h2>cytos</h2>
        <p>No slide open.</p>
        <p>
          <button type="button" onClick={() => host?.openSlideDialog()}>
            Open Slide…
          </button>{' '}
          or press ⌘O.
        </p>
      </div>
    );
  }
  if (error) {
    return (
      <div style={{ padding: 24 }}>
        <h2>could not open slide</h2>
        <p>
          <code>{slideUrl}</code>
        </p>
        <p>{error}</p>
        {!host && (
          <p>
            Is the data server running? <code>python tools/serve_slides.py</code>
          </p>
        )}
      </div>
    );
  }
  if (!slide || !settings) {
    return <div style={{ padding: 24 }}>loading {slideUrl} …</div>;
  }
  return (
    <>
      <SlideViewer
        slide={slide}
        settings={settings}
        features={features}
        initialView={initialView}
      />
      <Panel
        slide={slide}
        settings={settings}
        features={features}
        genes={genes}
        custom={custom}
        onChange={(key, patch) =>
          setSettings((prev) =>
            prev
              ? {
                  ...prev,
                  layers: {
                    ...prev.layers,
                    [key]: { ...prev.layers[key], ...patch },
                  },
                }
              : prev,
          )
        }
        onSectionChange={(name, patch) =>
          setSettings((prev) =>
            prev
              ? {
                  ...prev,
                  sections: {
                    ...prev.sections,
                    [name]: { ...prev.sections[name], ...patch },
                  },
                }
              : prev,
          )
        }
      />
    </>
  );
}
