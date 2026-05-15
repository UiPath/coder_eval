import { BindingsFile, FlowManifest, PreflightReport, UipConnection } from './types';
export interface LoadedProject {
    filPath: string;
    bindingsPath?: string;
    filSource: string;
    flowId: string;
    flowName: string;
    flowVersion: string;
    /**
     * Derived from the FIL's top-level `flow`/`action`/`trigger` declarations.
     * Kept under the `manifest` field for compatibility with the dispatcher
     * and preflight resolver, which were written against the legacy
     * `<Name>.manifest.flow` shape.
     */
    manifest: FlowManifest;
    bindings?: BindingsFile;
}
export declare function loadProject(projectDir: string, opts?: {
    filPath?: string;
    bindingsPath?: string;
}): LoadedProject;
export declare function preflight(project: LoadedProject, libraryDir: string, connections: UipConnection[] | null): PreflightReport;
