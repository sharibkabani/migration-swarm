// Not escapes: `as const`, a string that mentions @ts-ignore, and prose in a doc comment.
export const a = [1, 2] as const;
export const s = "// @ts-ignore is just text here";
/** Mentions any and @ts-ignore in prose, not as a directive. */
export function g(x: number): number { return x; }
