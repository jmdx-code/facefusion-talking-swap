"""Local temple skin correction, applied after enhancement; never copies the mouth."""
import cv2
import numpy as np

def temple_mask(shape, p, skin, generated_points=None):
    w=float(np.linalg.norm(p[16]-p[0]));h=float(np.linalg.norm(p[8]-p[27]))
    eye_axis=p[45]-p[36];angle=float(np.degrees(np.arctan2(eye_axis[1],eye_axis[0])))
    mask=np.zeros(shape[:2],np.float32)
    for jaw,brow in [(0,17),(16,26)]:
        center=p[jaw]*.55+p[brow]*.45
        cv2.ellipse(mask,tuple(center.round().astype(int)),(round(w*.125),round(h*.31)),angle,0,360,1,-1)
        center=p[brow].copy();center[1]-=h*.16
        cv2.ellipse(mask,tuple(center.round().astype(int)),(round(w*.14),round(h*.40)),angle,0,360,1,-1)
    mask=cv2.GaussianBlur(mask,(0,0),w*.022)
    # Original skin segmentation keeps hair, eyebrows and eyes out of the correction.
    mask*=cv2.GaussianBlur(skin.astype(np.float32),(0,0),2)
    protect=np.zeros(shape[:2],np.uint8)
    for landmarks in [p]+([] if generated_points is None else [generated_points]):
        for indices in [list(range(17,22)),list(range(22,27)),list(range(36,42)),list(range(42,48))]:
            cv2.fillConvexPoly(protect,cv2.convexHull(np.rint(landmarks[indices]).astype(np.int32)),255)
            cv2.polylines(protect,[np.rint(landmarks[indices]).astype(np.int32)],False,255,max(3,round(w*.025)))
    protect=cv2.dilate(protect,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(11,11)))
    mask*=1-cv2.GaussianBlur(protect.astype(np.float32)/255,(0,0),2)
    mask[protect>0]=0
    yy=np.arange(shape[0])[:,None]
    eye_y=float((p[36,1]+p[45,1])*.5)
    mask*=np.clip((eye_y+h*.32-yy)/(h*.10),0,1)
    mask[mask<.003]=0
    return np.clip(mask,0,1)

def original_skin(original,generated,p,mask,skin):
    source=cv2.bilateralFilter(original,9,28,12)
    a=cv2.cvtColor(source,cv2.COLOR_BGR2LAB).astype(np.float32)
    b=cv2.cvtColor(generated,cv2.COLOR_BGR2LAB).astype(np.float32)
    w=float(np.linalg.norm(p[16]-p[0]));h=float(np.linalg.norm(p[8]-p[27]));x=float(p[27,0])
    yy,xx=np.ogrid[:original.shape[0],:original.shape[1]]
    ref=(abs(xx-x)<w*.22)&(yy>min(p[19,1],p[24,1])-h*.36)&(yy<min(p[19,1],p[24,1])-h*.08)&(skin>.8)
    if np.count_nonzero(ref)<30:return generated.copy()
    delta=np.median((b-a)[ref],axis=0)
    adjusted=cv2.cvtColor(np.clip(a+delta,0,255).round().astype('uint8'),cv2.COLOR_LAB2BGR)
    alpha=mask[:,:,None]
    return np.clip(generated*(1-alpha)+adjusted*alpha,0,255).round().astype('uint8')


def local_skin(original, generated, points, mask, skin):
    """Restrict the expensive bilateral/LAB operation to the affected ROI."""
    w=float(np.linalg.norm(points[16]-points[0])); h=float(np.linalg.norm(points[8]-points[27])); x=float(points[27,0])
    bx,by,bw,bh=cv2.boundingRect((mask>0).astype('uint8'))
    if not bw or not bh:
        return generated.copy()
    x1=max(0,int(min(bx,x-w*.22))-16); x2=min(generated.shape[1],int(max(bx+bw,x+w*.22))+17)
    y1=max(0,int(min(by,min(points[19,1],points[24,1])-h*.36))-16)
    y2=min(generated.shape[0],int(max(by+bh,min(points[19,1],points[24,1])-h*.08))+17)
    result=generated.copy()
    result[y1:y2,x1:x2]=original_skin(original[y1:y2,x1:x2],generated[y1:y2,x1:x2],points-[x1,y1],mask[y1:y2,x1:x2],skin[y1:y2,x1:x2])
    return result


def correct_temple(original, generated, face, points=None):
    from facefusion.face_landmarker import detect_face_landmark
    from facefusion.face_helper import convert_to_face_landmark_5, warp_face_by_face_landmark_5
    from facefusion.face_masker import create_region_mask
    if face is None:
        return generated
    # The preview can still be BGRA when no source face has been supplied.
    # Work on color channels and preserve its alpha byte-for-byte.
    if generated.shape[2] == 4:
        output = generated.copy()
        output[:, :, :3] = correct_temple(original[:, :, :3], generated[:, :, :3], face, points)
        return output
    original = original[:, :, :3]
    points=face.landmark_set['68'] if points is None else points
    generated_points, score=detect_face_landmark(generated, face.bounding_box, face.angle)
    if score < .5:
        return generated
    crop, matrix=warp_face_by_face_landmark_5(original,convert_to_face_landmark_5(points),'ffhq_512',(512,512))
    skin=cv2.warpAffine(create_region_mask(crop,['skin']),matrix,(original.shape[1],original.shape[0]),flags=cv2.INTER_LINEAR|cv2.WARP_INVERSE_MAP)
    mask=temple_mask(generated.shape,points,skin,generated_points)
    return local_skin(original,generated,points,mask,skin)
