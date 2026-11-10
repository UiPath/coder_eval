import {
    armColor,
    sparkDomain,
    sparkLine,
    type SeriesArm,
} from "@/lib/series";

const WIDTH = 96;
const HEIGHT = 28;

export function Sparkline({
    arms,
    sparks,
}: {
    arms: readonly SeriesArm[];
    sparks: Record<string, readonly (number | null)[]>;
}) {
    const perArm = arms.map((a) =>
        (sparks[a.id] ?? []).filter((v): v is number => v != null),
    );
    if (perArm.every((vs) => vs.length < 2)) return null;
    const all = perArm.flat();
    const domain = sparkDomain(all);
    return (
        <svg
            role="img"
            aria-label={`Pass rate over the last ${Math.max(...arms.map((a) => (sparks[a.id] ?? []).length))} runs`}
            width={WIDTH}
            height={HEIGHT}
            viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        >
            {arms.map((a, i) => {
                const { d, last } = sparkLine(
                    sparks[a.id] ?? [],
                    WIDTH,
                    HEIGHT,
                    domain,
                );
                const color = armColor(i, arms.length);
                return (
                    <g key={a.id}>
                        {d && (
                            <path
                                d={d}
                                fill="none"
                                stroke={color}
                                strokeWidth={1.5}
                                strokeLinejoin="round"
                                strokeLinecap="round"
                            />
                        )}
                        {last && (
                            <circle cx={last.x} cy={last.y} r={2} fill={color} />
                        )}
                    </g>
                );
            })}
        </svg>
    );
}
