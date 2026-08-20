/**
 * Load a `.cytos` slide and hand it to the viewer.
 *
 * Two ways in, one viewer. In a browser tab the slide is a URL, named with
 * `?slide=<url>`, e.g.
 *   http://localhost:5173/?slide=http://127.0.0.1:8787/pancreas_ffpe.cytos
 * In the desktop shell it is a directory on disk, chosen from File ▸ Open
 * Slide… or named on the command line. Neither has a default: with no slide
 * named you get the welcome screen, the way the Qt viewer's welcome window
 * works. A hard-coded fallback used to fill in for a browser tab, which only
 * ever made sense against one developer's own test server.
 *
 * Wiring only: find the slide, load it, hold the settings, and keep the
 * session written. What loading means is `io/slide.ts`; what the settings
 * mean is `core/session.ts`; where the bytes come from is `io/read.ts`;
 * where a session is kept is `io/sessions.ts`.
 */

import { useEffect, useMemo, useRef, useState } from 'react';

import {
  applySession,
  cameraFromView,
  collectSession,
  defaultSettings,
  pointsKey,
  segmentsKey,
  rectFromCamera,
  slugify,
  DEFAULT_SESSION_NAME,
  SESSION_FORMAT,
  type SavedCamera,
  type SavedSession,
  type SlideSettings,
  type ViewRect,
} from './core/session';
import { loadFeatures, type FeatureTable } from './io/features';
import { loadGenes, type GeneTable } from './io/points';
import { desktopHost } from './io/host';
import { presenceFor } from './io/presence';
import { readerFor } from './io/read';
import { recentSlides, type RecentSlide } from './io/recents';
import { sessionStoreFor, type SessionInfo } from './io/sessions';
import { loadSlide, type LoadedSlide } from './io/slide';
import { SlideViewer, type CameraView, type Recenter, type TakeShot } from './render/scene';
import type { CustomColors } from './ui/color-picker';
import { Panel } from './ui/panel';
import { ScaleBar } from './ui/scale-bar';
import { SessionPicker } from './ui/session-picker';
import { Welcome } from './ui/welcome';

/** How long after the last change the session is written. Long enough that
 * dragging a slider is one write, short enough that closing the tab a second
 * later has already saved. */
const SAVE_DELAY = 800;

/** How often the camera is checked for having moved. Panning is not React
 * state (see `render/scene.tsx`), so nothing else would notice it. */
const CAMERA_CHECK = 2000;

/** How often the picker's thumbnail is taken, at most. Grabbing the frame
 * reads pixels back off the GPU, which stalls the pipeline (Chromium says so
 * out loud), and a session is written every couple of seconds while you pan
 * — so this is what keeps a preview from costing a stall per pan. A picture
 * a few seconds behind the view is one nobody can tell from a fresh one. */
const SHOT_INTERVAL = 5000;

function sameCamera(a: SavedCamera | null, b: SavedCamera | null): boolean {
  if (!a || !b) return a === b;
  return (
    a.center[0] === b.center[0] &&
    a.center[1] === b.center[1] &&
    a.size[0] === b.size[0] &&
    a.size[1] === b.size[1]
  );
}

export default function App() {
  const host = desktopHost();
  const params = new URLSearchParams(window.location.search);
  // ?session=<name> — which saved view this tab opens. Without it a tab
  // takes the most recent one no other tab is already in, so a second tab on
  // the same slide is a second view rather than a fight over one file.
  const urlSession = params.get('session');
  // ?view=x,y,zoom (full-res pixel coords) — start somewhere specific. An
  // explicit instruction, so it wins over the session's own camera.
  const viewParam = params.get('view')?.split(',').map(Number);
  const urlView =
    viewParam?.length === 3 && viewParam.every(Number.isFinite)
      ? (viewParam as [number, number, number])
      : undefined;

  const [slideUrl, setSlideUrl] = useState<string | null>(
    () => params.get('slide')?.replace(/\/+$/, '') ?? null,
  );
  const [slide, setSlide] = useState<LoadedSlide | null>(null);
  // Slides opened before. The shell keeps the list (its Open Recent menu is
  // built from the same file); a tab keeps its own.
  const recents = useMemo(() => recentSlides(), []);
  const [recentList, setRecentList] = useState<RecentSlide[]>([]);
  const [settings, setSettings] = useState<SlideSettings | null>(null);
  const [features, setFeatures] = useState<Record<string, FeatureTable | null>>({});
  const [genes, setGenes] = useState<Record<string, GeneTable | null>>({});
  const [error, setError] = useState<string | null>(null);
  // One pool of user-created colors for the whole window, shared by every
  // picker — a color mixed while tuning one layer is a one-click swatch on
  // the next. Saved in the session as `custom_colors`, as in Qt.
  const [customColors, setCustomColors] = useState<string[]>([]);
  const custom: CustomColors = {
    colors: customColors,
    add: (hex) =>
      setCustomColors((prev) => (prev.includes(hex) ? prev : [...prev, hex])),
    remove: (hex) => setCustomColors((prev) => prev.filter((c) => c !== hex)),
  };

  // Sessions: the saved views of this slide, which one is open, and the
  // document it was loaded from — kept whole so fields only the Qt viewer
  // understands survive a save here.
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [sessionName, setSessionName] = useState<string>(DEFAULT_SESSION_NAME);
  const savedDoc = useRef<SavedSession | null>(null);
  const store = useMemo(() => (slideUrl ? sessionStoreFor(slideUrl) : null), [slideUrl]);
  /** Names the other windows on this slide hold. */
  const [inUse, setInUse] = useState<string[]>([]);
  /** True while the session picker is up — the slide is loaded but no
   * session has been chosen, so nothing is drawn yet. */
  const [picking, setPicking] = useState(false);
  const presence = useMemo(() => (slideUrl ? presenceFor(slideUrl) : null), [slideUrl]);
  /** The camera as last written, so panning can be noticed without React
   * hearing about every frame of it. */
  const savedCamera = useRef<SavedCamera | null>(null);
  /** Set while a session is being applied: the change that follows is the
   * session arriving, not the user doing something, and rewriting a file to
   * say what it already says would only disturb the "most recent" order. */
  const justApplied = useRef(false);

  // The region the viewer opens on — the session's saved camera, or null to
  // fit the whole slide. Turned into a zoom by the scene, which is the only
  // thing that knows how big the canvas really is.
  const [openingRect, setOpeningRect] = useState<ViewRect | null>(null);

  // Where the camera is (written by the scene on every move, read by the
  // minimap on its own timer) and where the minimap has asked it to go.
  const camera = useRef<CameraView | null>(null);
  /** The scene's way of handing back the frame on screen, for the session's
   * thumbnail. Null until the viewer is up. */
  const shot = useRef<TakeShot | null>(null);
  /** When the last thumbnail was taken, so `SHOT_INTERVAL` can be kept. */
  const shotAt = useRef(0);
  const [recenter, setRecenter] = useState<Recenter | null>(null);
  const recenterTo = (x: number, y: number) => {
    const view = camera.current;
    if (!view) return;
    // Keep the zoom the user is at — a navigator moves the camera, it does
    // not re-frame the slide.
    setRecenter((prev) => ({ x, y, zoom: view.zoom, seq: (prev?.seq ?? 0) + 1 }));
  };

  // The menu, the save timer and the camera watcher all act on whatever is
  // loaded *now*, so they read refs rather than closing over a render's
  // values — every one of them is registered once, not per change.
  const slideRef = useRef<LoadedSlide | null>(null);
  slideRef.current = slide;
  const settingsRef = useRef<SlideSettings | null>(null);
  settingsRef.current = settings;
  const colorsRef = useRef<string[]>(customColors);
  colorsRef.current = customColors;
  const nameRef = useRef(sessionName);
  nameRef.current = sessionName;
  const storeRef = useRef(store);
  storeRef.current = store;
  const presenceRef = useRef(presence);
  presenceRef.current = presence;

  /** The camera in the session's own units, or null before the first frame. */
  const currentCamera = (): SavedCamera | null => {
    const view = camera.current;
    const loaded = slideRef.current;
    if (!view || !loaded) return null;
    return cameraFromView(view.x, view.y, view.zoom, view.width, view.height, loaded.pixelSize);
  };

  const saveSession = async () => {
    const active = storeRef.current;
    const state = settingsRef.current;
    if (!active || !state) return;
    const cam = currentCamera();
    const doc = collectSession(nameRef.current, savedDoc.current, state, cam, colorsRef.current);
    savedDoc.current = doc;
    savedCamera.current = cam;
    try {
      await active.save(nameRef.current, doc);
    } catch (err) {
      // A read-only slide is a normal thing to be looking at; losing the
      // view state is not a reason to interrupt anyone. Same call as Qt's.
      console.warn(`could not save session "${nameRef.current}":`, err);
    }
    // Then the picker's thumbnail, from the frame on screen — after the
    // document, and never in its way: a preview is worth having, and worth
    // nothing next to the view state itself. Qt writes the two together for
    // the same reason (`save_session`).
    try {
      if (Date.now() - shotAt.current < SHOT_INTERVAL) return;
      shotAt.current = Date.now();
      const frame = await shot.current?.();
      if (frame) await active.saveShot(nameRef.current, frame);
    } catch (err) {
      console.warn(`no thumbnail for session "${nameRef.current}":`, err);
    }
  };

  /** Open a saved session onto an already-loaded slide. */
  const openSession = async (name: string, loaded: LoadedSlide) => {
    const active = storeRef.current;
    // A session that won't load is a session ignored, never a slide that
    // won't open — the same rule `load_session` follows in Python, and the
    // reason a browser with its database switched off still works.
    const doc = active
      ? await active.load(name).catch((err) => {
          console.warn(`could not read session "${name}":`, err);
          return null;
        })
      : null;
    justApplied.current = true;
    shotAt.current = 0;
    savedDoc.current = doc;
    savedCamera.current = doc?.camera ?? null;
    const applied = applySession(loaded.manifest, doc);
    setSettings(applied.settings);
    setCustomColors(applied.customColors);
    setSessionName(name);
    setRecenter(null);
    setOpeningRect(doc?.camera ? rectFromCamera(doc.camera, loaded.pixelSize) : null);
    // Claim it, so the other windows grey the name out.
    presenceRef.current?.announce(name);
  };

  const refreshSessions = async () => {
    const active = storeRef.current;
    if (active) setSessions(await active.list());
  };

  useEffect(() => {
    if (!host) return;
    host.initialSlide().then((dir) => {
      if (dir) setSlideUrl(dir);
    });
    host.onOpenSlide(setSlideUrl);
    host.onResetSettings(() => {
      const loaded = slideRef.current;
      if (!loaded) return;
      // Back to exactly what `cytos-import` wrote. The session file stays —
      // you named it — and simply comes to hold the defaults, which is what
      // the next save writes. Qt's "Reset to Slide Defaults" does the same.
      setSettings(defaultSettings(loaded.manifest));
      setCustomColors([]);
      setRecenter(null);
      setOpeningRect(null);
    });
  }, [host]);

  useEffect(() => {
    recents.list().then(setRecentList).catch(() => {});
  }, [recents, slideUrl]);

  useEffect(() => {
    if (!presence) return;
    presence.onChange((names) => {
      setInUse(names);
      // Another window opening or closing a session usually means it created
      // one too, so the list is re-read rather than left to go stale.
      refreshSessions();
    });
    return () => presence.close();
  }, [presence]);

  useEffect(() => {
    if (!slideUrl) return;
    let cancelled = false;
    setSlide(null);
    setSettings(null);
    setFeatures({});
    setGenes({});
    setError(null);
    setRecenter(null);
    setSessions([]);
    setPicking(false);
    loadSlide(readerFor(slideUrl))
      .then(async (loaded) => {
        if (cancelled) return;
        setSlide(loaded);
        const found = await storeRef.current!.list().catch(() => [] as SessionInfo[]);
        const taken = (await presenceRef.current?.inUse().catch(() => [])) ?? [];
        if (cancelled) return;
        setSessions(found);
        setInUse(taken);
        // Which saved view to come back to is a question with more than one
        // answer, so it is asked rather than guessed — the picker, with a
        // thumbnail of each. Two exceptions, both Qt's: a session named
        // outright (`?session=`) is an instruction, and a slide nobody has
        // saved a view of has nothing to choose between, so it opens
        // straight into "default".
        if (urlSession) await openSession(urlSession, loaded);
        else if (found.length === 0) await openSession(DEFAULT_SESSION_NAME, loaded);
        else setPicking(true);
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
        // A slide that no longer opens stops being offered. Missing ones the
        // shell simply skips (an unmounted drive is not gone); this is for
        // the one that answered and could not be read.
        recents.forget(slideUrl);
      });
    recents.remember(slideUrl).catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [slideUrl]);

  // Write the session a moment after the last change. Not on a Save button:
  // a viewer that edits nothing has no unsaved work worth a decision.
  useEffect(() => {
    if (!settings) return;
    if (justApplied.current) {
      justApplied.current = false;
      return;
    }
    const timer = window.setTimeout(() => {
      saveSession().then(refreshSessions);
    }, SAVE_DELAY);
    return () => window.clearTimeout(timer);
  }, [settings, customColors, sessionName]);

  // Panning and zooming never reach React, so the camera is looked at on a
  // timer instead — and written only when it actually moved.
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (!settingsRef.current) return;
      const cam = currentCamera();
      if (cam && !sameCamera(cam, savedCamera.current)) saveSession();
    }, CAMERA_CHECK);
    const onHide = () => {
      if (settingsRef.current) saveSession();
    };
    // `pagehide`, not `beforeunload`: it fires when a tab is closed *and*
    // when the page goes into the background cache, and it is the one the
    // desktop shell's window close raises too.
    window.addEventListener('pagehide', onHide);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('pagehide', onHide);
    };
  }, []);

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
      <Welcome
        desktop={Boolean(host)}
        recents={recentList}
        onOpen={(id) => {
          // In the shell the main process opens it — it owns the window's
          // slide and the read allow-list. In a tab this is just the URL.
          if (host) host.openRecent(id);
          else setSlideUrl(id);
        }}
      />
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
  if (!slide) {
    return <div style={{ padding: 24 }}>loading {slideUrl} …</div>;
  }
  if (picking) {
    return (
      <SessionPicker
        slideName={slide.manifest.name}
        sessions={sessions}
        inUse={inUse}
        loadShot={(name) => store?.loadShot(name) ?? Promise.resolve(null)}
        onOpen={(name) => {
          setPicking(false);
          openSession(name, slide).then(refreshSessions);
        }}
        onCreate={async (name) => {
          const active = storeRef.current;
          if (!active) return;
          // A name already taken is left exactly as it is — silently
          // replacing someone's saved view is the one outcome worth ruling
          // out — and the row is simply there to open.
          if (!sessions.some((s) => slugify(s.name) === slugify(name))) {
            await active
              .save(name, { cytos_session: SESSION_FORMAT, name })
              .catch((err) => console.warn(`could not create session "${name}":`, err));
          }
          refreshSessions();
        }}
        onDelete={(name) => {
          storeRef.current
            ?.remove(name)
            .catch((err) => console.warn(`could not delete session "${name}":`, err))
            .then(refreshSessions);
        }}
        onCancel={() => {
          // Nothing chosen, so no window is bound to a session and there is
          // nothing to show. Qt does not open the slide either.
          setPicking(false);
          setSlide(null);
          setSlideUrl(null);
        }}
      />
    );
  }
  if (!settings) {
    return <div style={{ padding: 24 }}>loading {slideUrl} …</div>;
  }
  return (
    <>
      <SlideViewer
        slide={slide}
        settings={settings}
        features={features}
        genes={genes}
        initialView={urlView}
        openingRect={openingRect}
        camera={camera}
        recenter={recenter}
        shot={shot}
      />
      <ScaleBar slide={slide} camera={camera} />
      <Panel
        slide={slide}
        settings={settings}
        features={features}
        genes={genes}
        custom={custom}
        camera={camera}
        onRecenter={recenterTo}
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
