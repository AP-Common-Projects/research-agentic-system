import { useState } from 'react';
import type { FinalReport, Grade, GradedFinding } from '../lib/api';
import { EmptyState, GradeBadge, Panel, PanelHeader, StatTile } from '../components/primitives';
import { decimal } from '../lib/format';

const AXIS_LABEL: Record<string, string> = {
  corroboration: 'Corroboration',
  consistency: 'Consistency',
  recency: 'Recency',
  effect_size: 'Effect size',
};

const AXIS_MEANING: Record<string, string> = {
  corroboration: 'How many distinct channels back the claim',
  consistency: 'How tightly their outlier scores agree',
  recency: 'How much of the evidence is recent',
  effect_size: 'How large the strongest outlier is',
};

const GRADE_ORDER: Grade[] = ['strong', 'moderate', 'weak'];

function EvidenceAxes({ evidence }: { evidence: GradedFinding['evidence'] }) {
  const axes = Object.keys(AXIS_LABEL).filter((k) => evidence[k]);

  if (axes.length === 0) {
    return <p className="text-xs text-ink-3">No per-axis evidence recorded for this finding.</p>;
  }

  return (
    <dl className="grid gap-3 sm:grid-cols-2">
      {axes.map((axis) => (
        <div key={axis} className="rounded-md border border-line bg-page px-3 py-2">
          <div className="flex items-center justify-between gap-2">
            <dt className="text-xs font-medium text-ink-2">{AXIS_LABEL[axis]}</dt>
            <dd>
              <GradeBadge grade={evidence[axis] as Grade} size="sm" />
            </dd>
          </div>
          <p className="mt-1 text-[11px] text-ink-3">{AXIS_MEANING[axis]}</p>
        </div>
      ))}
    </dl>
  );
}

function FindingRow({ finding }: { finding: GradedFinding }) {
  const [open, setOpen] = useState(false);
  const maxOutlier = finding.evidence.max_outlier_score;

  return (
    <li className="px-4 py-3">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-start gap-3 text-left"
      >
        <span className="mt-0.5 shrink-0">
          <GradeBadge grade={finding.grade} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-sm text-ink">{finding.claim}</span>
          <span className="mt-1 block font-mono text-[11px] text-ink-3">
            {finding.supporting_channel_ids.length} channel
            {finding.supporting_channel_ids.length === 1 ? '' : 's'}
            {finding.pattern_type ? ` · ${finding.pattern_type}` : ''}
            {typeof maxOutlier === 'number' ? ` · peak ${decimal(maxOutlier)}×` : ''}
          </span>
        </span>
        <span aria-hidden className="mt-1 shrink-0 font-mono text-[10px] text-ink-3">
          {open ? '−' : '+'}
        </span>
      </button>

      {open ? (
        <div className="mt-3 pl-1">
          <EvidenceAxes evidence={finding.evidence} />
          {finding.supporting_channel_ids.length > 0 ? (
            <div className="mt-3">
              <p className="mb-1.5 font-mono text-[10px] tracking-wide text-ink-3 uppercase">
                Supporting channels
              </p>
              <div className="flex flex-wrap gap-1.5">
                {finding.supporting_channel_ids.map((id) => (
                  <code
                    key={id}
                    className="rounded border border-line bg-page px-1.5 py-0.5 font-mono text-[11px] text-ink-2"
                  >
                    {id}
                  </code>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

export function ReportView({ report }: { report: FinalReport }) {
  const counts = GRADE_ORDER.map((grade) => ({
    grade,
    count: report.findings.filter((f) => f.grade === grade).length,
  }));

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-3 gap-3">
        {counts.map(({ grade, count }) => (
          <StatTile
            key={grade}
            label={`${grade} findings`}
            value={count}
            accent={`var(--grade-${grade})`}
          />
        ))}
      </div>

      <Panel>
        <PanelHeader title="Summary" hint={`Generated ${report.generated_at}`} />
        <div className="px-4 py-4">
          <p className="text-sm leading-relaxed text-ink-2">
            {report.summary || 'No summary text was produced.'}
          </p>
        </div>
      </Panel>

      <Panel>
        <PanelHeader
          title={`Findings (${report.findings.length})`}
          hint="Grades are computed from the evidence, not written by the model."
        />
        {report.findings.length > 0 ? (
          <ul className="divide-y divide-line">
            {report.findings.map((finding, i) => (
              <FindingRow key={`${finding.claim}-${i}`} finding={finding} />
            ))}
          </ul>
        ) : (
          <EmptyState title="No findings" />
        )}
      </Panel>

      {report.cannot_determine.length > 0 ? (
        <Panel>
          <PanelHeader
            title="Cannot determine"
            hint="Questions this run is not equipped to answer — stated rather than guessed."
          />
          <ul className="divide-y divide-line">
            {report.cannot_determine.map((item, i) => (
              <li key={i} className="px-4 py-2.5 text-sm text-ink-2">
                {item}
              </li>
            ))}
          </ul>
        </Panel>
      ) : null}
    </div>
  );
}
