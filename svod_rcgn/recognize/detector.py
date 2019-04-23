import glob
import os
import pickle

import cv2
import numpy as np
import six
from openvino import inference_engine as ie
from sklearn import neighbors
from sklearn import svm

from svod_rcgn.recognize import nets, defaults
from svod_rcgn.tools import images, bg_remove
from svod_rcgn.tools.print import print_fun


class DetectorClassifiers:
    def __init__(self):
        self.classifiers = []
        self.classifier_names = []
        self.embedding_sizes = []
        self.class_names = None
        self.class_stats = None


def add_detector_args(parser):
    parser.add_argument(
        '--threshold',
        type=float,
        default=defaults.THRESHOLD,
        help='Threshold for detecting faces',
    )
    parser.add_argument(
        '--debug',
        help='Full debug output for each detected face.',
        action='store_true',
    )


def detector_args(args):
    return Detector(
        device=args.device,
        face_detection_path=args.face_detection_path,
        model_dir=args.model_dir,
        classifiers_dir=args.classifiers_dir,
        bg_remove_path=args.bg_remove_path,
        threshold=args.threshold,
        debug=args.debug,
    )


class Detector(object):
    def __init__(
            self,
            device=defaults.DEVICE,
            face_detection_path=defaults.FACE_DETECTION_PATH,
            model_dir=defaults.MODEL_DIR,
            classifiers_dir=defaults.CLASSIFIERS_DIR,
            bg_remove_path=bg_remove.DEFAULT_BG_REMOVE_DIR,
            threshold=defaults.THRESHOLD,
            debug=defaults.DEBUG,
            loaded_plugin=None,
    ):

        self._initialized = False
        self.device = device
        self.face_detection_path = face_detection_path
        self.model_dir = model_dir
        self.classifiers_dir = classifiers_dir
        self.bg_remove_path = bg_remove_path
        self.use_classifiers = False
        self.classifiers = DetectorClassifiers()
        self.threshold = threshold
        self.debug = debug
        self.loaded_plugin = loaded_plugin

    def init(self):

        if self._initialized:
            return
        self._initialized = True

        extensions = os.environ.get('INTEL_EXTENSIONS_PATH')
        if self.loaded_plugin is not None:
            plugin = self.loaded_plugin
        else:
            plugin = ie.IEPlugin(device=self.device)

        if extensions and "CPU" in self.device:
            for ext in extensions.split(':'):
                print_fun("LOAD extension from {}".format(ext))
                plugin.add_cpu_extension(ext)

        print_fun('Load FACE DETECTION')
        weights_file = self.face_detection_path[:self.face_detection_path.rfind('.')] + '.bin'
        net = ie.IENetwork(self.face_detection_path, weights_file)
        self.face_detect = nets.FaceDetect(plugin, net)

        if self.model_dir:
            print_fun('Load FACENET model')
            model_file = os.path.join(self.model_dir, "facenet.xml")
            weights_file = os.path.join(self.model_dir, "facenet.bin")
            net = ie.IENetwork(model_file, weights_file)
            self.facenet_input = list(net.inputs.keys())[0]
            outputs = list(iter(net.outputs))
            self.facenet_output = outputs[0]
            self.face_net = plugin.load(net)

        self.bg_remove = bg_remove.get_driver(self.bg_remove_path)

        self.load_classifiers()

    def load_classifiers(self):

        if not bool(self.model_dir):
            return

        self.use_classifiers = False

        classifiers = glob.glob(os.path.join(self.classifiers_dir, "*.pkl"))

        if len(classifiers) > 0:
            new = DetectorClassifiers()
            for clfi, clf in enumerate(classifiers):
                # print_fun(clfi, clf)
                # Load classifier
                with open(clf, 'rb') as f:
                    print_fun('Load CLASSIFIER %s' % clf)
                    opts = {'file': f}
                    if six.PY3:
                        opts['encoding'] = 'latin1'
                    (clf, class_names, class_stats) = pickle.load(**opts)
                    if isinstance(clf, svm.SVC):
                        embedding_size = clf.shape_fit_[1]
                        classifier_name = "SVM"
                        classifier_name_log = "SVM classifier"
                    elif isinstance(clf, neighbors.KNeighborsClassifier):
                        embedding_size = clf._fit_X.shape[1]
                        classifier_name = "kNN"
                        classifier_name_log = "kNN (neighbors %d) classifier" % clf.n_neighbors
                    else:
                        # try embedding_size = 512
                        embedding_size = 512
                        classifier_name = "%d" % clfi
                        classifier_name_log = type(clf)
                    print_fun('Loaded %s, embedding size: %d' % (classifier_name_log, embedding_size))
                    if new.class_names is None:
                        new.class_names = class_names
                    elif class_names != new.class_names:
                        raise RuntimeError("Different class names in classifiers")
                    if new.class_stats is None:
                        new.class_stats = class_stats
                    elif class_stats != new.class_stats:
                        raise RuntimeError("Different class stats in classifiers")
                    new.classifier_names.append(classifier_name)
                    new.embedding_sizes.append(embedding_size)
                    new.classifiers.append(clf)

            self.classifiers = new
            self.use_classifiers = True

    def detect_faces(self, frame, threshold=0.5):
        if self.bg_remove is not None:
            bounding_boxes_frame = self.bg_remove.apply_mask(frame)
        else:
            bounding_boxes_frame = frame
        return openvino_detect(self.face_detect, bounding_boxes_frame, threshold)

    def inference_facenet(self, img):
        output = self.face_net.infer(inputs={self.facenet_input: img})
        return output[self.facenet_output]

    def process_output(self, output, bbox):
        detected_indices = []
        label_strings = []
        probs = []
        prob_detected = True
        summary_overlay_label = ""

        for clfi, clf in enumerate(self.classifiers.classifiers):

            try:
                output = output.reshape(1, self.classifiers.embedding_sizes[clfi])
                predictions = clf.predict_proba(output)
            except ValueError as e:
                # Can not reshape
                print_fun("ERROR: Output from graph doesn't consistent with classifier model: %s" % e)
                continue

            best_class_indices = np.argmax(predictions, axis=1)

            if isinstance(clf, neighbors.KNeighborsClassifier):

                def process_index(idx):
                    cnt = self.classifiers.class_stats[best_class_indices[idx]]['embeddings']
                    (closest_distances, neighbors_indices) = clf.kneighbors(output, n_neighbors=cnt)
                    eval_values = closest_distances[:, 0]
                    first_cnt = 0
                    for i in neighbors_indices[0]:
                        if clf._y[i] != best_class_indices[idx]:
                            break
                        first_cnt += 1
                    # probability:
                    # first matched embeddings
                    # less than 25% is 0%, more than 75% is 100%
                    # multiplied by distance coefficient:
                    # 0.5 and less is 100%, 0.83 and more is 0%
                    prob = max(0, min(1, 2 * first_cnt / cnt - .5)) * max(0, min(1, 2.5 - eval_values[idx] * 3))
                    label_debug = '%.3f %d/%d' % (
                        eval_values[idx],
                        first_cnt, cnt,
                    )
                    return prob, label_debug

            elif isinstance(clf, svm.SVC):

                def process_index(idx):
                    eval_values = predictions[np.arange(len(best_class_indices)), best_class_indices]
                    label_debug = '%.1f%%' % (eval_values[idx] * 100)
                    return max(0, min(1, eval_values[idx] * 10)), label_debug

            else:

                print_fun("ERROR: Unsupported model type: %s" % type(clf))
                continue

            for i in range(len(best_class_indices)):
                detected_indices.append(best_class_indices[i])
                overlay_label = self.classifiers.class_names[best_class_indices[i]]
                summary_overlay_label = overlay_label
                prob, label_debug = process_index(i)
                probs.append(prob)
                if prob <= 0:
                    prob_detected = False
                classifier_name = self.classifiers.classifier_names[clfi]
                label_debug_info = \
                    '%s: %.1f%% %s (%s)' % (classifier_name, prob * 100, overlay_label, label_debug)
                if self.debug:
                    label_strings.append(label_debug_info)
                elif len(label_strings) == 0:
                    label_strings.append(overlay_label)
                # print_fun(label_debug_info)

        # detected if all classes are the same, and all probs are more than 0
        detected = len(set(detected_indices)) == 1 and prob_detected
        mean_prob = sum(probs) / len(probs) if detected else 0

        if self.debug:
            if detected:
                label_strings.append("Summary: %.1f%% %s" % (mean_prob * 100, summary_overlay_label))
            else:
                label_strings.append("Summary: not detected")

        thin = not detected
        color = (0, 0, 255) if thin else (0, 255, 0)

        bb = bbox.astype(int)
        bounding_boxes_overlay = {
            'bb': bb,
            'thin': thin,
            'color': color,
        }

        overlay_label_str = ""
        if self.debug:
            if len(label_strings) > 0:
                overlay_label_str = "\n".join(label_strings)
        elif detected:
            overlay_label_str = label_strings[0]

        overlay_label = None
        if overlay_label_str != "":
            overlay_label = {
                'label': overlay_label_str,
                'left': bb[0],
                'top': bb[1],
                'right': bb[2],
                'bottom': bb[3],
                'color': color,
            }

        return bounding_boxes_overlay, overlay_label, mean_prob

    def process_frame(self, frame, frame_rate=None, overlays=True):

        bounding_boxes_detected = self.detect_faces(frame, self.threshold)
        bounding_boxes_overlays = []
        labels = []
        if self.use_classifiers:

            imgs = images.get_images(frame, bounding_boxes_detected)

            for img_idx, img in enumerate(imgs):

                label_strings = []

                # Infer
                # t = time.time()
                # Convert BGR to RGB
                img = img[:, :, ::-1]
                img = img.transpose([2, 0, 1]).reshape([1, 3, 160, 160])
                output = self.inference_facenet(img)
                # LOG.info('facenet: %.3fms' % ((time.time() - t) * 1000))
                # output = output[facenet_output]

                face_overlay, face_label, _ = self.process_output(output, bounding_boxes_detected[img_idx])
                bounding_boxes_overlays.append(face_overlay)
                if face_label:
                    labels.append(face_label)

        # LOG.info('facenet: %.3fms' % ((time.time() - t) * 1000))
        if overlays:
            self.add_overlays(frame, bounding_boxes_overlays, labels, frame_rate)
        return bounding_boxes_overlays, labels

    @staticmethod
    def add_overlays(frame, boxes, labels=None, frame_rate=None, align_to_right=True):
        add_overlays(
            frame, boxes,
            frame_rate=frame_rate,
            labels=labels,
            align_to_right=align_to_right
        )


def add_overlays(frame, boxes, frame_rate=None, labels=None, align_to_right=True):
    if boxes is not None:
        for face in boxes:
            face_bb = face['bb'].astype(int)
            cv2.rectangle(
                frame,
                (face_bb[0], face_bb[1]), (face_bb[2], face_bb[3]),
                face['color'], 1 if face['thin'] else 2,
            )

    frame_avg = (frame.shape[1] + frame.shape[0]) / 2
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_size = frame_avg / 1300
    font_thickness = 2 if frame_avg > 1000 else 1
    font_inner_padding_w, font_inner_padding_h = 5, 5

    if frame_rate is not None and frame_rate != 0:
        fps_txt = "%d fps" % frame_rate
        _, flh = cv2.getTextSize(fps_txt, font, font_size, thickness=font_thickness)[0]
        cv2.putText(
            frame, fps_txt,
            (font_inner_padding_w, font_inner_padding_h + flh),
            font, font_size, (0, 255, 0),
            thickness=font_thickness, lineType=2
        )

    if labels:
        for l in labels:
            if l is None:
                continue
            strs = l['label'].split('\n')
            str_w, str_h = 0, 0
            widths = []
            for i, line in enumerate(strs):
                lw, lh = cv2.getTextSize(line, font, font_size, thickness=font_thickness)[0]
                str_w = max(str_w, lw)
                str_h = max(str_h, lh)
                widths.append(lw)
            str_h = int(str_h * 1.6) # line height

            to_right = l['left'] + str_w > frame.shape[1] - font_inner_padding_w

            top = l['top'] - int((len(strs) - 0.5) * str_h)
            if top < str_h + font_inner_padding_h:
                top = min(l['bottom'] + int(str_h * 1.2), frame.shape[0] - str_h * len(strs) + font_inner_padding_h)

            for i, line in enumerate(strs):
                if align_to_right:
                    # all align to right box border
                    left = (l['right'] - widths[i] - font_inner_padding_w) if to_right else l['left'] + font_inner_padding_w
                else:
                    # move left each string if it's ending not places on the frame
                    left = frame.shape[1] - widths[i] - font_inner_padding_w \
                        if l['left'] + widths[i] > frame.shape[1] - font_inner_padding_w \
                        else l['left'] + font_inner_padding_w

                cv2.putText(
                    frame, line,
                    (
                        left,
                        int(top + i * str_h),
                    ),
                    font,
                    font_size,
                    l['color'],
                    thickness=font_thickness, lineType=cv2.LINE_AA
                )



def openvino_detect(face_detect, frame, threshold):
    inference_frame = cv2.resize(frame, face_detect.input_size, interpolation=cv2.INTER_AREA)
    inference_frame = np.transpose(inference_frame, [2, 0, 1]).reshape(*face_detect.input_shape)
    outputs = face_detect(inference_frame)
    outputs = outputs.reshape(-1, 7)
    bboxes_raw = outputs[outputs[:, 2] > threshold]
    bounding_boxes = bboxes_raw[:, 3:7]
    bounding_boxes[:, 0] = bounding_boxes[:, 0] * frame.shape[1]
    bounding_boxes[:, 2] = bounding_boxes[:, 2] * frame.shape[1]
    bounding_boxes[:, 1] = bounding_boxes[:, 1] * frame.shape[0]
    bounding_boxes[:, 3] = bounding_boxes[:, 3] * frame.shape[0]

    return bounding_boxes
