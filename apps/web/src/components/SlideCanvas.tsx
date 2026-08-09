import type { Citation, SlideDetail } from "../types";

export function SlideCanvas({ slide, citation }: { slide: SlideDetail; citation?: Citation | null }) {
  return (
    <div className="slide-canvas" aria-label={`第 ${slide.slide_no} 页预览`}>
      <img src={slide.preview_url} alt={slide.title || `第 ${slide.slide_no} 页`} />
      {citation?.bbox && (
        <div
          className="evidence-highlight"
          aria-label="引用证据高亮"
          style={{
            left: `${citation.bbox.x * 100}%`, top: `${citation.bbox.y * 100}%`,
            width: `${citation.bbox.w * 100}%`, height: `${citation.bbox.h * 100}%`
          }}
        />
      )}
    </div>
  );
}

