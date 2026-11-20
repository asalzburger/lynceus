"""Conservative native-PDF plot recovery. Unsupported geometry is deferred.

PDF paths are rendered geometry, not original measurement files. This module
does not infer histogram counts, curve fits, error semantics or legend meaning.
"""
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pymupdf

VERSION = "lynceus.pdf-vector/v1"


class Deferred(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def figure_rect(bbox, page):
    """Normalize Docling bottom-left boxes and CLI top-left PDF rectangles."""
    if bbox is None:
        return pymupdf.Rect(page.rect)
    if isinstance(bbox, dict):
        origin = str(bbox.get("coord_origin", "TOPLEFT")).upper()
        if origin.endswith("BOTTOMLEFT"):
            r = pymupdf.Rect(bbox["l"], page.rect.height-bbox["t"], bbox["r"], page.rect.height-bbox["b"])
        elif origin.endswith("TOPLEFT"):
            r = pymupdf.Rect(bbox["l"], bbox["t"], bbox["r"], bbox["b"])
        else:
            raise Deferred("unsupported-coordinates", "Unknown figure coordinate origin")
    else:
        r = pymupdf.Rect(bbox)
    if not all(math.isfinite(v) for v in r) or r.is_empty or not page.rect.contains(r):
        raise Deferred("invalid-region", "Figure bounds must be a finite rectangle inside the page")
    return r


def _color(color):
    return tuple(round(float(v), 2) for v in (color or (0, 0, 0)))


def _lines(paths):
    for pid, path in paths:
        for sid, item in enumerate(path["items"]):
            if item[0] == "l":
                yield pid, sid, item[1], item[2]


def _number(value):
    value = value.replace("−", "-").replace("–", "-")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _frames(paths, region):
    found = {}
    for pid, path in paths:
        if path.get("color") is None:
            continue
        r = path["rect"]
        if r.width < 45 or r.height < 35 or not region.contains(r):
            continue
        rectangle = any(i[0] in {"qu", "re"} for i in path["items"])
        if not rectangle:
            # Matplotlib and similar exporters often emit four separate edges.
            edges = [i for i in path["items"] if i[0] == "l"]
            horizontal = [i for i in edges if abs(i[1].y-i[2].y)<.03 and abs(i[1].x-i[2].x)>.9*r.width]
            vertical = [i for i in edges if abs(i[1].x-i[2].x)<.03 and abs(i[1].y-i[2].y)>.9*r.height]
            rectangle = len(horizontal)>=2 and len(vertical)>=2
        if rectangle:
            found.setdefault(tuple(round(x, 2) for x in r), (pid, r))
    return list(found.values())


def _ticks(paths, frame, axis):
    found = {}
    for pid, sid, a, b in _lines(paths):
        if axis == "x":
            position, length = (a.x+b.x)/2, abs(a.y-b.y)
            valid = abs(a.x-b.x)<.03 and abs(max(a.y,b.y)-frame.y1)<.08 and 1<length<.12*frame.height
        else:
            position, length = (a.y+b.y)/2, abs(a.x-b.x)
            valid = abs(a.y-b.y)<.03 and abs(min(a.x,b.x)-frame.x0)<.08 and 1<length<.12*frame.width
        if valid:
            key = round(position, 2)
            if key not in found or length>found[key]["length"]:
                found[key] = {"position": position, "length": length, "path_id": pid, "segment_id": sid}
    return list(found.values())


def _fit_axis(words, paths, frame, axis):
    ticks = _ticks(paths, frame, axis)
    if len(ticks)<3:
        raise Deferred("missing-ticks", f"Not enough native {axis.upper()} tick marks")
    anchors = {}
    for w in words:
        value = _number(w[4])
        if value is None:
            continue
        if axis == "x":
            # Some native font boxes start just above the axis even though the
            # printed tick text is below it (common in the ATLAS reference).
            inside = frame.y1-2<w[1]<frame.y1+24 and (w[1]+w[3])/2>frame.y1+1.5 and frame.x0-4<(w[0]+w[2])/2<frame.x1+4
            position = (w[0]+w[2])/2
        else:
            inside = frame.x0-38<w[0]<frame.x0 and w[2]<frame.x0+1 and frame.y0-8<(w[1]+w[3])/2<frame.y1+8
            position = (w[1]+w[3])/2
        if not inside:
            continue
        tick = min(ticks, key=lambda t:abs(t["position"]-position))
        # The longest ticks win near a printed label; digit ink is not centered
        # perfectly in its PDF text bounding box.
        near = [t for t in ticks if abs(t["position"]-position)<3]
        if near:
            tick = max(near, key=lambda t:t["length"])
        if abs(tick["position"]-position)>3:
            continue
        key = round(tick["position"], 2)
        if key in anchors:
            raise Deferred("ambiguous-tick-text", "Multiple native numbers map to one tick; split superscripts or labels require review")
        anchors[key] = {"position": tick["position"], "value": value, "text": w[4], "text_bbox": list(w[:4]), "tick_path_id": tick["path_id"], "tick_segment_id": tick["segment_id"]}
    anchors = sorted(anchors.values(), key=lambda a:a["position"])
    if len(anchors)<3:
        raise Deferred("missing-native-labels", f"At least three unambiguous native {axis.upper()} labels are required; OCR is not used in this pass")
    positions = np.array([a["position"] for a in anchors])
    values = np.array([a["value"] for a in anchors])
    if not (np.all(np.diff(values)>0) or np.all(np.diff(values)<0)):
        raise Deferred("nonmonotonic-tick-labels", f"Native {axis.upper()} tick values are not monotonic")
    span = frame.width if axis == "x" else frame.height
    tolerance = max(.15, span*.001)
    fits = []
    for scale in ("linear", "log10"):
        if scale == "log10" and np.any(values<=0):
            continue
        transformed = np.log10(values) if scale == "log10" else values
        slope, offset = np.polyfit(positions, transformed, 1)
        if abs(slope)<1e-15:
            continue
        residual = (transformed-(slope*positions+offset))/slope
        fits.append((float(np.sqrt(np.mean(residual**2))), float(np.max(np.abs(residual))), scale, float(slope), float(offset)))
    fits.sort()
    if not fits or fits[0][1]>tolerance:
        raise Deferred("unsupported-axis-scale", f"Native {axis.upper()} labels fit neither a linear nor a log10 axis")
    best = fits[0]
    if len(fits)>1 and fits[1][1]<=tolerance and fits[1][0]<max(1e-7, best[0]*4):
        raise Deferred("ambiguous-axis-scale", f"Cannot distinguish linear and logarithmic {axis.upper()} calibration")
    return {"scale": best[2], "slope": best[3], "offset": best[4], "anchors": anchors, "max_residual_pdf_points": best[1], "rms_residual_pdf_points": best[0], "tolerance_pdf_points": tolerance, "confidence": max(.05, 1-best[1]/tolerance)}


def _value(axis, position):
    v = axis["slope"]*position+axis["offset"]
    if axis["scale"] == "log10":
        if abs(v)>300:
            raise Deferred("extreme-calibration", "Log calibration exceeds numerical range")
        return 10**v
    return v


def _label(words, frame, axis, region):
    selected = []
    for w in words:
        if _number(w[4]) is not None or not pymupdf.Rect(w[:4]).intersects(region):
            continue
        if axis == "x":
            valid = frame.y1+14<w[1]<frame.y1+42 and frame.x0-5<(w[0]+w[2])/2<frame.x1+5
        else:
            valid = frame.x0-55<w[0] and w[2]<frame.x0-20 and frame.y0-12<(w[1]+w[3])/2<frame.y1+8
        if valid:
            selected.append(w)
    # Preserve native block / line / word order, including rotated Y labels.
    value = " ".join(w[4] for w in sorted(selected,key=lambda w:w[5:8])).strip()
    if len(value)>160:
        value = ""
    match = re.match(r"^(.*)\s*[\[(]([^()[\]]+)[\])]\s*$", value)
    return {"label": (match.group(1).strip() if match else value) or axis.upper()+" — verify label", "unit": match.group(2).strip() if match else ""}


def _legends(paths, words, frame):
    regions = []
    for _, path in paths:
        r = path["rect"]
        if path.get("color") is None or r.width<20 or r.height<12 or r.get_area()>.65*frame.get_area() or not frame.contains(r):
            continue
        if any(i[0] in {"qu","re"} for i in path["items"]) and sum(r.contains(pymupdf.Rect(w[:4])) for w in words)>=2:
            regions.append(r)
    return regions


def _marker(path):
    r = path["rect"]
    if not .8<r.width<11 or not .8<r.height<11 or not .55<r.width/r.height<1.8:
        return None
    items = path["items"]
    if len(items)==4 and all(i[0]=="c" for i in items) and abs(r.width-r.height)<.1:
        kind = "circle"
    elif len(items)==1 and items[0][0] in {"re","qu"}:
        kind = "square"
    elif 2<=len(items)<=6 and all(i[0]=="l" for i in items) and (path.get("fill") is not None or path.get("closePath") or math.dist(items[0][1],items[-1][2])<.08):
        vertices = [list(items[0][1])]+[list(i[2]) for i in items]
        if math.dist(vertices[0],vertices[-1])<.08:
            vertices.pop()
        # A square path can begin halfway along an edge. Remove collinear
        # intermediate vertices rather than treating it as a pentagon.
        changed=True
        while changed and len(vertices)>3:
            changed=False
            for i,p in enumerate(vertices):
                a,b=vertices[i-1],vertices[(i+1)%len(vertices)]
                u,v=[p[j]-a[j] for j in (0,1)],[b[j]-p[j] for j in (0,1)]
                if abs(u[0]*v[1]-u[1]*v[0])<.03 and u[0]*v[0]+u[1]*v[1]>=0:
                    vertices.pop(i);changed=True;break
        if len(vertices) not in {3,4}:
            return None
        kind = "triangle" if len(vertices)==3 else "square"
    else:
        return None
    if path.get("fill") is None and path.get("color") is None:
        return None
    fill=path.get("fill")
    open_marker=fill is None or (min(fill)>.95 and path.get("color") is not None)
    color = _color(path.get("color") if open_marker else fill)
    style = "open" if open_marker else "filled"
    return kind, style, color, [(r.x0+r.x1)/2,(r.y0+r.y1)/2], (r.width+r.height)/2


def _marker_parts(path):
    """Preserve disconnected native subpaths, including packed open markers."""
    runs=[]
    run=[]
    end=None
    for item in path["items"]:
        if item[0] in {"re","qu"}:
            if run:runs.append(run);run=[]
            runs.append([item]);end=None
            continue
        if item[0] not in {"l","c"}:
            if run:runs.append(run);run=[]
            end=None
            continue
        if end is not None and math.dist(end,item[1])>.05:
            runs.append(run);run=[]
        run.append(item)
        end=item[-1]
    if run:runs.append(run)
    for index,items in enumerate(runs):
        if len(items)==1 and items[0][0] in {"re","qu"}:
            rect=items[0][1] if items[0][0]=="re" else items[0][1].rect
        else:
            points=[p for item in items for p in item[1:]]
            rect=pymupdf.Rect(min(p.x for p in points),min(p.y for p in points),max(p.x for p in points),max(p.y for p in points))
        yield index,dict(path,items=items,rect=rect)


def _bezier(a,b,c,d,tolerance=.1,depth=0):
    a,b,c,d = [np.asarray(p,dtype=float) for p in (a,b,c,d)]
    chord = d-a
    length = np.linalg.norm(chord)
    cross=lambda u,v:u[0]*v[1]-u[1]*v[0]
    distance = max(abs(cross(chord,b-a)),abs(cross(chord,c-a)))/length if length>1e-12 else max(np.linalg.norm(b-a),np.linalg.norm(c-a))
    if distance<=tolerance or depth>=12:
        return [a.tolist(),d.tolist()]
    ab,bc,cd = (a+b)/2,(b+c)/2,(c+d)/2
    abc,bcd = (ab+bc)/2,(bc+cd)/2
    mid = (abc+bcd)/2
    left = _bezier(a,ab,abc,mid,tolerance,depth+1)
    right = _bezier(mid,bcd,cd,d,tolerance,depth+1)
    return left+right[1:]


def _subpaths(path):
    run = []
    for item in path["items"]:
        if item[0] == "l":
            points = [list(item[1]),list(item[2])]
        elif item[0] == "c":
            points = _bezier(*item[1:])
        else:
            if run:yield run
            run = []
            continue
        if run and math.dist(run[-1],points[0])>.05:
            yield run
            run = []
        run.extend(points if not run else points[1:])
    if run:yield run


def _error_caps(paths, markers, color):
    result = []
    colors={pid:_color(p.get("color")) for pid,p in paths}
    bins=defaultdict(list)
    for pid,sid,a,b in _lines(paths):
        if colors[pid]==color and abs(a.y-b.y)<.03 and .2<abs(a.x-b.x)<14:
            bins[math.floor((a.x+b.x))].append((pid,sid,a,b))
    for m in markers:
        x,y = m["point"]
        diameter = m["diameter"]
        caps = {}
        radius=max(.15,.15*diameter)
        candidates=[c for key in range(math.floor(2*(x-radius)),math.floor(2*(x+radius))+1) for c in bins.get(key,[])]
        for pid,sid,a,b in candidates:
            if abs(a.y-b.y)<.03 and .3*diameter<abs(a.x-b.x)<1.25*diameter and abs((a.x+b.x)/2-x)<max(.15,.15*diameter) and abs(a.y-y)<4*diameter:
                caps[round(a.y,3)] = {"path_id":pid,"segment_id":sid,"a":list(a),"b":list(b)}
        values = sorted(caps.items())
        if len(values)==2 and values[0][0]<y<values[1][0]:
            result.append({"top":values[0][1],"bottom":values[1][1]})
        else:
            result.append(None)
    return result


def recover(pdf_path, page_number, bbox=None):
    """Return native geometry or a recorded deferral; never return guessed axes."""
    inspection = {"version":VERSION,"page":page_number,"accepted":False}
    try:
        with pymupdf.open(pdf_path) as pdf:
            if not 1<=page_number<=len(pdf):
                raise Deferred("invalid-page","PDF page is out of range")
            page = pdf[page_number-1]
            if page.rotation:
                raise Deferred("rotated-page","Rotated pages require an explicitly normalized figure")
            region = figure_rect(bbox,page)
            inspection["figure_bbox_pdf"] = list(region)
            paths = [(i,d) for i,d in enumerate(page.get_drawings()) if region.contains(d["rect"])]
            words = page.get_text("words")
            inspection["path_count"] = len(paths)
            if not paths:
                raise Deferred("no-vector-paths","No native drawing paths in the figure region")
            frames = []
            failures = []
            for frame_id,frame in _frames(paths,region):
                try:
                    x = _fit_axis(words,paths,frame,"x")
                    y = _fit_axis(words,paths,frame,"y")
                    frames.append((frame_id,frame,x,y))
                except Deferred as exc:
                    failures.append({"code":exc.code,"message":str(exc)})
            inspection["frame_attempts"] = failures
            if not frames:
                raise Deferred("no-calibrated-frame","No single rectangular XY frame with native, consistent tick labels was recovered")
            if len(frames)>1:
                raise Deferred("multiple-panels","More than one calibrated panel; split the figure region before numerical extraction")
            frame_id,frame,xaxis,yaxis = frames[0]
            for w in words:
                if _number(w[4]) is not None and frame.x1+1<w[0]<frame.x1+36 and frame.y0<(w[1]+w[3])/2<frame.y1:
                    raise Deferred("additional-y-axis","Numbers beside the right axis require explicit series/axis assignment")
            for img in page.get_images():
                if any((r & frame).get_area()>.05*frame.get_area() for r in page.get_image_rects(img[0])):
                    raise Deferred("raster-in-plot","A raster image overlaps the data region; native paths alone may omit plotted content")
            legends = _legends(paths,words,frame)
            groups = defaultdict(list)
            marker_paths = set()
            for pid,path in paths:
                parts=list(_marker_parts(path))
                only_markers=bool(parts)
                for part_id,part in parts:
                    m = _marker(part)
                    if not m:
                        only_markers=False
                        continue
                    kind,style,color,point,diameter = m
                    if not frame.contains(pymupdf.Point(*point)) or any(r.contains(pymupdf.Point(*point)) for r in legends):
                        continue
                    groups[(kind,style,color,round(diameter))].append({"point":point,"path_id":pid,"subpath_id":part_id,"diameter":diameter})
                if only_markers:marker_paths.add(pid)
            series = []
            warnings = ["Native PDF geometry is a candidate extraction. Verify series identity, units, masks and missing/overlapping marks before sign-off."]
            for key,markers in sorted(groups.items(),key=lambda pair:pair[0]):
                if len(markers)<2:
                    warnings.append("An isolated marker-like object was omitted; inspect for missing data or legend symbols.")
                    continue
                markers.sort(key=lambda m:_value(xaxis,m["point"][0]))
                caps = _error_caps(paths,markers,key[2])
                series.append({"kind":"markers","label":f"Markers {len(series)+1} ({key[1]} {key[0]})","color":list(key[2]),"points":[m["point"] for m in markers],"path_ids":[m["path_id"] for m in markers],"subpath_ids":[m["subpath_id"] for m in markers],"marker_shape":key[0],"error_caps":caps})
                if any(c is not None for c in caps) and not all(c is not None for c in caps):
                    warnings.append("Only some Y-error-bar caps were recovered; no complete error array will be invented.")
            seen_curves={}
            for pid,path in paths:
                if pid in marker_paths or pid==frame_id or path.get("color") is None or path.get("fill") is not None:
                    continue
                if any(item[0] in {"qu","re"} for item in path["items"]):
                    continue
                if path.get("closePath"):
                    continue
                for points in _subpaths(path):
                    arr = np.asarray(points)
                    width,height = np.ptp(arr,axis=0)
                    if len(points)<2 or max(width/frame.width,height/frame.height)<.15:
                        continue
                    # Constant lines and the rectangular grid cannot be separated
                    # reliably by geometry alone. They are deliberately deferred.
                    if width<.1 or height<.1:
                        continue
                    if not all(frame.contains(pymupdf.Point(*p)) for p in points):
                        continue
                    if any(r.contains(pymupdf.Point(*p)) for r in legends for p in points):
                        warnings.append("A drawn path overlapping the legend was omitted; inspect for missing curve portions.")
                        continue
                    if np.any(np.diff(arr[:,0])<-.05):
                        warnings.append("A non-monotonic path was omitted; loops and boundary polygons need manual selection.")
                        continue
                    fingerprint=(_color(path["color"]),tuple(tuple(round(v,3) for v in p) for p in points))
                    if fingerprint in seen_curves:
                        seen_curves[fingerprint]["path_ids"].append(pid)
                        continue
                    series.append({"kind":"curve","label":f"Curve {sum(s['kind']=='curve' for s in series)+1} (drawn path)","color":list(_color(path["color"])),"points":points,"path_ids":[pid],"commands":dict(Counter(i[0] for i in path["items"]))})
                    seen_curves[fingerprint]=series[-1]
            if not series:
                raise Deferred("no-native-series","No supported marker group or curve survived frame/legend/axis filtering")
            if len(series)>256 or sum(len(s["points"]) for s in series)>200_000:
                raise Deferred("geometry-limit","The native candidate exceeds the numerical plot limits")
            if any(s["kind"]=="curve" for s in series):
                warnings.append("Curve samples describe the published drawing, not additional experimental observations; Bezier paths are sampled to 0.1 PDF-point flatness.")
            labels = {a:_label(words,frame,a,region) for a in ("x","y")}
            if any("verify label" in labels[a]["label"] for a in labels):
                warnings.append("Axis labels/units were not fully recovered and need reviewer input.")
            inspection.update(accepted=True,reason_code="native-geometry",message="Native axes and plotted paths recovered",frame_path_id=frame_id,frame_bbox_pdf=list(frame),axes={"x":xaxis,"y":yaxis},labels=labels,legend_masks_pdf=[list(r) for r in legends],series=series,warnings=list(dict.fromkeys(warnings)),coordinate_system="PDF points, top-left origin",page_raster_image_count=len(page.get_images()),requires_signoff=True)
            return inspection
    except Deferred as exc:
        inspection.update(reason_code=exc.code,message=str(exc))
        return inspection
