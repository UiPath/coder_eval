import { Suspense } from "react";
import { TableScroll } from "@/app/_components/scroll-table";
import { groupSeries, unknownSeries } from "@/lib/series";
import { loadUnlistedSeriesKeys } from "@/lib/series-data";
import {
    SeriesRow,
    SeriesRowSkeleton,
} from "./_components/series-row";

export const dynamic = "force-dynamic";

const TH = "py-3 px-4 font-medium";

function GroupHeader({ group }: { group: string }) {
    return (
        <tr className="bg-gray-50 border-b border-gray-200">
            <th
                colSpan={5}
                scope="colgroup"
                className="py-2 px-4 text-left text-xs font-semibold uppercase tracking-wide text-gray-500"
            >
                {group}
            </th>
        </tr>
    );
}

async function UnlistedSeries() {
    const keys = await loadUnlistedSeriesKeys();
    if (keys.length === 0) return null;
    return (
        <tbody>
            <GroupHeader group="Other" />
            {keys.map((key) => {
                const def = unknownSeries(key);
                return (
                    <Suspense
                        key={key}
                        fallback={<SeriesRowSkeleton def={def} />}
                    >
                        <SeriesRow def={def} />
                    </Suspense>
                );
            })}
        </tbody>
    );
}

export default function SeriesPage() {
    return (
        <div className="space-y-5">
            <div className="space-y-1">
                <h1 className="text-xl font-semibold text-gray-900">Series</h1>
                <p className="text-sm text-gray-500">
                    Scheduled runs outside the skills nightly, one trend per
                    series.
                </p>
            </div>
            <TableScroll>
                <table className="w-full text-sm">
                    <thead>
                        <tr className="bg-gray-50 border-b border-gray-200 text-left text-gray-600">
                            <th className={TH}>Series</th>
                            <th className={TH}>Schedule</th>
                            <th className={TH}>Last run</th>
                            <th className={TH}>Latest</th>
                            <th className={TH}>Last 8 runs</th>
                        </tr>
                    </thead>
                    {groupSeries().map(({ group, series }) => (
                        <tbody key={group}>
                            <GroupHeader group={group} />
                            {series.map((def) => (
                                <Suspense
                                    key={def.key}
                                    fallback={<SeriesRowSkeleton def={def} />}
                                >
                                    <SeriesRow def={def} />
                                </Suspense>
                            ))}
                        </tbody>
                    ))}
                    <Suspense fallback={null}>
                        <UnlistedSeries />
                    </Suspense>
                </table>
            </TableScroll>
        </div>
    );
}
