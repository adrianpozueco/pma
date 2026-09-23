import type { CSSProperties } from "react";

const paths: Record<string, React.ReactNode> = {
  arrow: (
    <>
      <path d="M4 12h15m-6-6 6 6-6 6" />
    </>
  ),
  back: <path d="m14 6-6 6 6 6" />,
  upload: (
    <>
      <path d="M12 16V4m-5 5 5-5 5 5M4 16v4h16v-4" />
    </>
  ),
  cloud: (
    <path d="M7 18h11a4 4 0 0 0 .7-7.94A7 7 0 0 0 5.2 8.3 5 5 0 0 0 7 18Z" />
  ),
  file: (
    <>
      <path d="M14 3H5v18h14V8l-5-5Z" />
      <path d="M14 3v5h5M8 12h8m-8 4h5" />
    </>
  ),
  check: <path d="m5 12 4 4L19 6" />,
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  shield: (
    <>
      <path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z" />
      <path d="m8 12 3 3 5-6" />
    </>
  ),
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v6m0-10v.2" />
    </>
  ),
  engine: (
    <>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="2" />
      <path d="m12 3 3 6m6 3-6 3m-3 6-3-6m-6-3 6-3m9-3-3 6m3 6-6-3m-6 3 3-6m-3-6 6 3" />
    </>
  ),
  boiler: (
    <>
      <rect x="6" y="6" width="12" height="15" rx="2" />
      <path d="M9 3V1m6 2V1M6 12h12m-9 5h6" />
    </>
  ),
  oven: (
    <>
      <rect x="3" y="4" width="18" height="17" rx="2" />
      <path d="M3 9h18M7 6.5h.1m4 0h.1M7 13h10v4H7z" />
    </>
  ),
  layers: (
    <>
      <path d="m12 3 10 5-10 5L2 8l10-5Zm-9 9 9 5 9-5M3 16l9 5 9-5" />
    </>
  ),
  search: (
    <>
      <circle cx="10" cy="10" r="6" />
      <path d="m15 15 6 6" />
    </>
  ),
  book: (
    <>
      <path d="M12 5v16M3 3c4 0 6 0 9 2 3-2 5-2 9-2v16c-4 0-6 0-9 2-3-2-5-2-9-2V3Z" />
    </>
  ),
  plane: <path d="m21 3-6 18-4-8-8-4L21 3Zm-10 10L21 3" />,
  /** Airliner silhouette, nose right. `plane` stays the send/deliver glyph. */
  aircraft: (
    <path d="M18.2 10.1H10.8L6.4 4.2H3.4l1.4 5.9-2.6 1.1 4.4 3.1h4.4l-4 5.9h3.4l4.8-5.9h3c2.6 0 3.9-.8 3.9-2.1s-1.3-2.1-3.9-2.1Z" />
  ),
  refresh: (
    <>
      <path d="M20 8a8 8 0 1 0 0 8M20 3v5h-5" />
    </>
  ),
  close: <path d="m6 6 12 12M6 18 18 6" />,
  calendar: (
    <>
      <rect x="3" y="5" width="18" height="16" rx="2" />
      <path d="M7 2v6m10-6v6M3 11h18m-13 4h4" />
    </>
  ),
};
export function Icon({
  name,
  size = 20,
  style,
}: {
  name: string;
  size?: number;
  style?: CSSProperties;
}) {
  return (
    <svg
      aria-hidden="true"
      width={size}
      height={size}
      style={style}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {paths[name] || paths.file}
    </svg>
  );
}

export function AircraftArtwork() {
  return (
    <svg
      className="aircraft-art"
      viewBox="0 0 440 240"
      fill="none"
      aria-hidden="true"
    >
      <circle
        cx="262"
        cy="122"
        r="102"
        stroke="currentColor"
        strokeDasharray="3 7"
        opacity=".3"
      />
      <circle cx="262" cy="122" r="75" stroke="currentColor" opacity=".13" />
      <path
        d="M54 164 204 121 238 31c4-9 11-10 12 0l-3 78 109-37 37-28 10 1-21 42-134 59-10 52-9 4-5-47-144 39-37-13-7-10 46 4Z"
        stroke="currentColor"
        strokeWidth="2"
        fill="currentColor"
        fillOpacity=".035"
      />
      <path
        d="m186 124 44 7 43-9M93 178l82-22"
        stroke="currentColor"
        opacity=".4"
      />
      <path
        d="M16 215h386M23 211v8m45-8v8m45-8v8m45-8v8m45-8v8m45-8v8m45-8v8m45-8v8m45-8v8"
        stroke="currentColor"
        opacity=".25"
      />
      <circle cx="270" cy="119" r="7" fill="#f1c933" />
      <circle cx="270" cy="119" r="15" stroke="#f1c933" opacity=".5" />
      <g className="aircraft-callout">
        <path d="M285 119h66v39" stroke="#f1c933" opacity=".8" />
        <rect x="286" y="158" width="130" height="35" rx="5" fill="#fff" />
        <text
          x="351"
          y="180"
          textAnchor="middle"
          fontFamily="Arial,sans-serif"
          fontSize="10"
          fill="#073590"
          fontWeight="700"
        >
          COMPONENT INSIGHT
        </text>
      </g>
    </svg>
  );
}
