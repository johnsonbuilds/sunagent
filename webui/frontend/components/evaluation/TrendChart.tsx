"use client";

export interface TrendPoint {
  label: string;
  value: number | null;
}

interface TrendChartProps {
  points: TrendPoint[];
  /** Formats the axis maximum and the tooltip title. */
  format: (value: number) => string;
  emptyLabel: string;
  height?: number;
}

// Wide enough that ``preserveAspectRatio="none"`` keeps the dots round once
// the viewBox is stretched to the card width.
const WIDTH = 360;
const PAD_TOP = 2;

/**
 * A dependency-free line/area chart.
 *
 * The viewBox is stretched (`preserveAspectRatio="none"`) and the stroke
 * carries `vector-effect="non-scaling-stroke"`, so the line keeps its
 * weight at any container width.
 */
export default function TrendChart({
  points,
  format,
  emptyLabel,
  height = 132,
}: TrendChartProps) {
  const usable = points.filter(
    (point) => point.value !== null && Number.isFinite(point.value),
  );
  if (usable.length === 0) {
    return <div className="eval-chart-empty">{emptyLabel}</div>;
  }

  const values = usable.map((point) => point.value as number);
  const max = Math.max(...values, 0);
  const ceiling = max > 0 ? max : 1;
  const inner = WIDTH - PAD_TOP * 2;
  const last = usable.length - 1;

  const coords = usable.map((point, index) => {
    const x = PAD_TOP + (last === 0 ? inner / 2 : (index / last) * inner);
    const y = height - PAD_TOP - ((point.value as number) / ceiling) *
      (height - PAD_TOP * 2);
    return { x, y, point };
  });
  const line = coords
    .map((item, index) => `${index === 0 ? "M" : "L"}${item.x.toFixed(2)} ` +
      `${item.y.toFixed(2)}`)
    .join(" ");
  const area = `${line} L${coords[coords.length - 1].x.toFixed(2)} ` +
    `${height - PAD_TOP} L${coords[0].x.toFixed(2)} ${height - PAD_TOP} Z`;

  return (
    <div className="eval-chart">
      <div className="eval-chart-max">{format(max)}</div>
      <svg
        className="eval-chart-svg"
        viewBox={`0 0 ${WIDTH} ${height}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`${usable[0].label} – ${usable[last].label}`}
      >
        <line
          className="eval-chart-grid"
          x1="0"
          x2={WIDTH}
          y1={height - PAD_TOP}
          y2={height - PAD_TOP}
          vectorEffect="non-scaling-stroke"
        />
        <path className="eval-chart-area" d={area} />
        <path
          className="eval-chart-line"
          d={line}
          vectorEffect="non-scaling-stroke"
        />
        {coords.map((item) => (
          <circle
            key={`${item.point.label}-${item.x}`}
            className="eval-chart-dot"
            cx={item.x}
            cy={item.y}
            r={1.4}
            vectorEffect="non-scaling-stroke"
          >
            <title>{`${item.point.label}: ${format(item.point.value as number)}`}</title>
          </circle>
        ))}
      </svg>
      <div className="eval-chart-axis">
        <span>{usable[0].label}</span>
        <span>{usable[last].label}</span>
      </div>
    </div>
  );
}
