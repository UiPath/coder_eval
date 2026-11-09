import Link from "next/link";
import { Fragment, Suspense } from "react";
import { TableScroll } from "@/app/_components/scroll-table";
import { SkelTable } from "@/app/_components/skeleton";
import { loadSeriesListing } from "@/lib/series-data";
import {
    SeriesRow,
    SeriesRowSkeleton,
} from "./_components/series-row";

export const dynamic = "force-dynamic";

const TH = "py-3 px-4 font-medium";

async function SeriesTable() {
    const { active, empty } = await loadSeriesListing();
    return (
        <div className="space-y-3">
            <TableScroll>
                <table className="w-full text-sm">
                    <thead>
                        <tr className="bg-gray-50 border-b border-gray-200 text-left text-gray-600">
                            <th className={TH}>Pipeline</th>
                            <th className={TH}>Last run</th>
                            <th className={TH}>Latest</th>
                            <th className={TH}>Last 8 runs</th>
                        </tr>
                    </thead>
                    <tbody>
                        {active.map((def) => (
                            <Suspense
                                key={def.key}
                                fallback={<SeriesRowSkeleton def={def} />}
                            >
                                <SeriesRow def={def} />
                            </Suspense>
                        ))}
                        {active.length === 0 && (
                            <tr>
                                <td
                                    colSpan={4}
                                    className="py-8 px-4 text-center text-gray-500"
                                >
                                    No scheduled pipeline has runs yet.
                                </td>
                            </tr>
                        )}
                    </tbody>
                </table>
            </TableScroll>
            {empty.length > 0 && (
                <p className="text-xs text-gray-500">
                    No runs yet:{" "}
                    {empty.map((def, i) => (
                        <Fragment key={def.key}>
                            {i > 0 && ", "}
                            <Link
                                href={`/series/${def.key}`}
                                className="hover:text-studio-blue"
                            >
                                {def.label}
                            </Link>
                        </Fragment>
                    ))}
                </p>
            )}
        </div>
    );
}

export default function SeriesPage() {
    return (
        <div className="space-y-5">
            <div className="space-y-1">
                <h1 className="text-xl font-semibold text-gray-900">Scheduled</h1>
                <p className="text-sm text-gray-500">
                    Pipelines that run on a schedule outside the skills
                    nightly, one trend each.
                </p>
            </div>
            <Suspense fallback={<SkelTable rows={4} />}>
                <SeriesTable />
            </Suspense>
        </div>
    );
}
