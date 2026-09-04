/**
 * Server-side pagination helpers.
 *
 * Kept out of the Pagination component file: that file is `"use client"`, and
 * anything exported from a client module cannot be called during server
 * rendering — even a pure function with no React in it.
 */

export type PaginationState = {
  page: number;
  pageSize: number;
  total: number;
  pages: number;
};

/** Read page/size from a server component's searchParams. */
export function readPageParams(
  searchParams: Record<string, string | string[] | undefined>,
  prefix = "",
  defaultSize = 50
): { page: number; pageSize: number } {
  const raw = (key: string) => {
    const value = searchParams[`${prefix}${key}`];
    return Array.isArray(value) ? value[0] : value;
  };
  const page = Math.max(1, Number(raw("page")) || 1);
  const requested = Number(raw("size")) || defaultSize;
  // Bounded so a crafted URL cannot request the entire table in one response.
  const pageSize = Math.min(Math.max(requested, 10), 500);
  return { page, pageSize };
}

/** Slice an in-memory array into a page, clamping an out-of-range page. */
export function paginate<T>(
  rows: T[],
  page: number,
  pageSize: number
): { visible: T[]; state: PaginationState } {
  const pages = Math.max(Math.ceil(rows.length / pageSize), 1);
  const safePage = Math.min(Math.max(page, 1), pages);
  return {
    visible: rows.slice((safePage - 1) * pageSize, safePage * pageSize),
    state: { page: safePage, pageSize, total: rows.length, pages },
  };
}
