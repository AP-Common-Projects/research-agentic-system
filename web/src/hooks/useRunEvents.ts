import { useEffect, useRef, useState } from 'react';
import { api, type NodeLogEntry } from '../lib/api';

type Connection = 'connecting' | 'live' | 'ended' | 'error';

/**
 * Live NodeLog stream for a run.
 *
 * Seeds from the REST log endpoint so a page load shows the whole run
 * immediately, then opens an SSE connection for everything after that cursor.
 * A multi-hour crawl emits a node every few minutes at most, so polling would
 * spend hundreds of requests to deliver a handful of transitions.
 */
export function useRunEvents(runId: string | undefined, enabled: boolean) {
  const [entries, setEntries] = useState<NodeLogEntry[]>([]);
  const [connection, setConnection] = useState<Connection>('connecting');
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!runId) return;

    let cancelled = false;
    setEntries([]);
    setConnection('connecting');

    // A ref, not state: the cursor advances on every message and nothing
    // renders from it, so keeping it in state would re-render the whole
    // stream view per line for no visual gain.
    const cursor = { value: 0 };

    api
      .logs(runId, 0)
      .then((seed) => {
        if (cancelled) return;
        setEntries(seed.entries);
        cursor.value = seed.cursor;

        if (!enabled) {
          setConnection('ended');
          return;
        }

        const source = new EventSource(api.eventsUrl(runId, cursor.value));
        sourceRef.current = source;

        source.addEventListener('node', (event) => {
          try {
            const payload = JSON.parse((event as MessageEvent).data);
            cursor.value = payload.cursor;
            setEntries((prev) => [...prev, payload.entry as NodeLogEntry]);
          } catch {
            /* a torn frame must not kill the stream */
          }
        });

        source.addEventListener('end', () => {
          setConnection('ended');
          source.close();
        });

        source.onopen = () => setConnection('live');
        source.onerror = () => {
          // EventSource reconnects on its own; only report a hard failure.
          if (source.readyState === EventSource.CLOSED) setConnection('error');
        };
      })
      .catch(() => {
        if (!cancelled) setConnection('error');
      });

    return () => {
      cancelled = true;
      sourceRef.current?.close();
      sourceRef.current = null;
    };
  }, [runId, enabled]);

  return { entries, connection };
}
