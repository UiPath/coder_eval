import { UipConnection } from './types';
export declare class ConnectionsFetchError extends Error {
    readonly stderr?: string | undefined;
    constructor(message: string, stderr?: string | undefined);
}
export declare function fetchConnections(opts?: {
    tenant?: string;
}): UipConnection[];
