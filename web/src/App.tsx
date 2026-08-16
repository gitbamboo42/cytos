/**
 * Step-1 probe: render a `.cytos` slide's image pyramid from a URL.
 *
 * Point it at a slide with `?slide=<url>`, e.g.
 *   http://localhost:5173/?slide=http://127.0.0.1:8787/breast_rep1_nozip.cytos
 * (that default is filled in when the param is missing). The slide server is
 * `tools/serve_slides.py`.
 */

import { PictureInPictureViewer } from '@hms-dbmi/viv';
import { useEffect, useState } from 'react';

import {
  CHANNEL_COLORS,
  fetchManifest,
  httpReadRange,
  imageLayers,
  openImagePyramid,
  stackChannels,
  type ImageLayerSpec,
  type SlideManifest,
} from './slide';

const DEFAULT_SLIDE = 'http://127.0.0.1:8787/breast_rep1_nozip.cytos';

interface LoadedSlide {
  manifest: SlideManifest;
  channels: ImageLayerSpec[];
  loader: ReturnType<typeof stackChannels>;
}

function useWindowSize() {
  const [size, setSize] = useState({ width: window.innerWidth, height: window.innerHeight });
  useEffect(() => {
    const onResize = () => setSize({ width: window.innerWidth, height: window.innerHeight });
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);
  return size;
}

export default function App() {
  const slideUrl =
    new URLSearchParams(window.location.search).get('slide')?.replace(/\/+$/, '') ??
    DEFAULT_SLIDE;
  const [slide, setSlide] = useState<LoadedSlide | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { width, height } = useWindowSize();

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const read = httpReadRange(slideUrl);
      const manifest = await fetchManifest(read);
      const channels = imageLayers(manifest);
      if (channels.length === 0) throw new Error('slide has no image layers');
      const perChannel = await Promise.all(
        channels.map((layer) => openImagePyramid(read, layer)),
      );
      if (!cancelled) {
        setSlide({ manifest, channels, loader: stackChannels(perChannel) });
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
  if (!slide) {
    return <div style={{ padding: 24 }}>loading {slideUrl} …</div>;
  }

  return (
    <PictureInPictureViewer
      loader={slide.loader}
      contrastLimits={slide.channels.map((c) => c.clim ?? [0, 65535])}
      colors={slide.channels.map((c) => CHANNEL_COLORS[c.colormap] ?? [255, 255, 255])}
      channelsVisible={slide.channels.map((c) => c.visible ?? true)}
      selections={slide.channels.map((_, i) => ({ c: i }))}
      overview={{}}
      overviewOn={false}
      height={height}
      width={width}
    />
  );
}
