"use strict";
/**
 * TypeScript interfaces for the .flow JSON format.
 * These cover the subset of the format relevant to flow2fil conversion.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.NODE_TYPES = void 0;
exports.isTriggerNode = isTriggerNode;
exports.isControlFlowNode = isControlFlowNode;
// Node type constants
exports.NODE_TYPES = {
    TRIGGER_MANUAL: 'core.trigger.manual',
    TRIGGER_SCHEDULED: 'core.trigger.scheduled',
    DECISION: 'core.logic.decision',
    SWITCH: 'core.logic.switch',
    MERGE: 'core.logic.merge',
    LOOP: 'core.logic.loop',
    DELAY: 'core.logic.delay',
    MOCK: 'core.logic.mock',
    TERMINATE: 'core.logic.terminate',
    END: 'core.control.end',
    SCRIPT: 'core.action.script',
    HTTP: 'core.action.http',
    HTTP_V2: 'core.action.http.v2',
    SUBFLOW: 'core.subflow',
};
function isTriggerNode(type) {
    return type.startsWith('core.trigger.');
}
function isControlFlowNode(type) {
    return type === exports.NODE_TYPES.DECISION
        || type === exports.NODE_TYPES.SWITCH
        || type === exports.NODE_TYPES.MERGE
        || type === exports.NODE_TYPES.LOOP
        || type === exports.NODE_TYPES.END
        || type === exports.NODE_TYPES.TERMINATE;
}
//# sourceMappingURL=flow-types.js.map