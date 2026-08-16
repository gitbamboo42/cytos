/**
 * Load a `.cytos` slide from a URL and hand it to the viewer.
 *
 * Point it at a slide with `?slide=<url>`, e.g.
 *   http://localhost:5173/?slide=http://127.0.0.1:8787/breast_rep1_nozip.cytos
 * (that default is filled in when the param is missing). The slide server is
 * `tools/serve_slides.py`.
 */

import { useEffect, useState } from 'react';

import { Panel } from './panel';
import { SegmentTileSource } from './segments';
import {
  fetchManifest,
  httpReadRange,
  imageLayers,
  openImagePyramid,
  segmentLayers,
  stackChannels,
} from './slide';
import { defaultSettings, type SlideSettings } from './state';
import { SlideViewer, type LoadedSlide } from './viewer';

const DEFAULT_SLIDE = 'http://127.0.0.1:8787/breast_rep1_nozip.cytos';

export default function App() {
  const params = new URLSearchParams(window.location.search);
  const slideUrl = params.get('slide')?.replace(/\/+$/, '') ?? DEFAULT_SLIDE;
  // ?view=x,y,zoom (full-res pixel coords) — start somewhere specific.
  const viewParam = params.get('view')?.split(',').map(Number);
  const initialView =
    viewParam?.length === 3 && viewParam.every(Number.isFinite)
      ? (viewParam as [number, number, number])
      : undefined;
  const [slide, setSlide] = useState<LoadedSlide | null>(null);
  const [settings, setSettings] = useState<SlideSettings | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const read = httpReadRange(slideUrl);
      const manifest = await fetchManifest(read);
      const channels = imageLayers(manifest);
      if (channels.length === 0) throw new Error('slide has no image layers');
      const pyramids = await Promise.all(
        channels.map((layer) => openImagePyramid(read, layer)),
      );
      const segments = segmentLayers(manifest).map(
        (spec) => new SegmentTileSource(read, spec),
      );
      if (!cancelled) {
        setSlide({
          manifest,
          channels,
          loader: stackChannels(pyramids.map((p) => p.levels)),
          pixelSize: pyramids[0].pixelSize,
          segments,
        });
        setSettings(defaultSettings(manifest));
      }
    })().catch((err) => {
      if (!cancelled) setError(String(err));
    });
    return () => {
      cancelled = true;
    };
  }, [slideUrl]);

  if (error) {
    return (
      <div style={{ padding: 24 }}>
        <h2>could not open slide</h2>
        <p>
          <code>{slideUrl}</code>
        </p>
        <p>{error}</p>
        <p>
          Is the data server running? <code>python tools/serve_slides.py</code>
        </p>
      </div>
    );
  }
  if (!slide || !settings) {
    return <div style={{ padding: 24 }}>loading {slideUrl} …</div>;
  }
  return (
    <>
      <SlideViewer slide={slide} settings={settings} initialView={initialView} />
      <Panel
        slide={slide}
        settings={settings}
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
