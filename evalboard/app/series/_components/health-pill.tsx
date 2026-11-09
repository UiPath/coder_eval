import { healthLabel, type SeriesHealth } from "@/lib/series";

const TONE: Record<SeriesHealth["status"], string> = {
    "on-time": "bg-green-50 text-green-700 border-green-200",
    missed: "bg-amber-50 text-amber-700 border-amber-200",
    paused: "bg-gray-50 text-gray-600 border-gray-200",
    "no-runs": "bg-gray-50 text-gray-600 border-gray-200",
    unscheduled: "bg-gray-50 text-gray-600 border-gray-200",
};

export function HealthPill({ health }: { health: SeriesHealth }) {
    return (
        <span
            className={`inline-flex items-center whitespace-nowrap px-2 py-0.5 text-xs rounded-full border ${TONE[health.status]}`}
        >
            {healthLabel(health)}
        </span>
    );
}
