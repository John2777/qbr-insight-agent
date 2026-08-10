import type { Citation, SlideDetail } from "../types";

export function SlideCanvas({ slide, citation }: { slide: SlideDetail; citation?: Citation | null }) {
  return (
    <div className="slide-canvas" aria-label={`Slide ${slide.slide_no} preview`}>
      <img src={slide.preview_url} alt={slide.title || `Slide ${slide.slide_no}`} decoding="async" fetchPriority="high" />
      {citation?.bbox && (
        <div
          className="evidence-highlight"
          aria-label="Cited evidence highlight"
          style={{
            left: `${citation.bbox.x * 100}%`, top: `${citation.bbox.y * 100}%`,
            width: `${citation.bbox.w * 100}%`, height: `${citation.bbox.h * 100}%`
          }}
        />
      )}
    </div>
  );
}
