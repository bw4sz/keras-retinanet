"""
On the fly generator. Crop out portions of a large image, and pass boxes and annotations. This follows the csv_generator template. Satifies the format in generator.py
"""
import pandas as pd

from .generator import Generator
from ..utils.image import read_image_bgr

import numpy as np
from PIL import Image
from six import raise_from

import csv
import sys
import os.path

from matplotlib import pyplot 
import matplotlib.patches as patches
import slidingwindow as sw
import itertools

#from lidar_crop import compute_chm

def expand_grid(data_dict):
    rows = itertools.product(*data_dict.values())
    return pd.DataFrame.from_records(rows, columns=data_dict.keys())

def load_csv(csv_data_file,res):
    
    #Read in data
    data=pd.read_csv(csv_data_file,index_col=0)    

    #Modify indices, which came from R, zero indexed in python
    data=data.set_index(data.index.values-1)
    data.numeric_label=data.numeric_label-1
    
    #Remove xmin==xmax
    data=data[data.xmin!=data.xmax]    
    data=data[data.ymin!=data.ymax]    

    ##Create bounding coordinates with respect to the crop for each box
    #Rescaled to resolution of the cells.Also note that python and R have inverse coordinate Y axis, flipped rotation.
    data['origin_xmin']=(data['xmin']-data['tile_xmin'])/res
    data['origin_xmax']=(data['xmin']-data['tile_xmin']+ data['xmax']-data['xmin'])/res
    data['origin_ymin']=(data['tile_ymax']-data['ymax'])/res
    data['origin_ymax']= (data['tile_ymax']-data['ymax']+ data['ymax'] - data['ymin'])/res  
    
    return(data)

#Find window indices
def compute_windows(image,pixels=250,overlap=0.05):
    im = Image.open(image)
    numpy_image = np.array(im)    
    windows = sw.generate(numpy_image, sw.DimOrder.HeightWidthChannel, pixels,overlap )
    return(windows)

#Get image from tile and window index
def retrieve_window(numpy_image,index,windows):
    crop=numpy_image[windows[index].indices()]
    return(crop)

def _read_classes(data):
    """ 
    """
    
    #Get unique classes
    uclasses=data.loc[:,['label','numeric_label']].drop_duplicates()
    
    # Define classes 
    classes = {}
    for index, row in uclasses.iterrows():
        classes[row.label] = row.numeric_label
    
    return(classes)


def _read_annotations(data,base_dir,windows,config):
    """ Create list of sliding windows to pass to reader. Named for legacy to match generator class.
    """
    
    #Create dictionary of windows for each image
    tile_windows={}
    tile_windows["image"]=list(data.rgb_path.unique())
    tile_windows["windows"]=np.arange(0,len(windows))
    
    #Expand grid
    tile_data=expand_grid(tile_windows)
    
    #Optionally subsample data based on config file. To increase efficiency, sample in order of preserving as many windows on the same tile, but shuffle within each tile
    
    if not config["subsample"] == "None":
        
        tile_data=tile_data.head(n=config["subsample"])
        groups = [df for _, df in tile_data.groupby('image')]
        groups=[x.sample(frac=1) for x in groups]
        tile_data=pd.concat(groups).reset_index(drop=True)
        
    image_dict=tile_data.to_dict("index")
    return(image_dict)
    
def fetch_annotations(image,index,annotations,windows,offset,patch_size):
    '''
    Find annotations that match the sliding window.
    Note that the window method is calculated once in train.py, this assumes all tiles have the same size and resolution
    offset: Number of meters to add to box edge to look for annotations
    '''

    #Find index of crop and create coordinate box
    x,y,w,h=windows[index].getRect()
    
    window_coords={}

    #top left
    window_coords["x1"]=x
    window_coords["y1"]=y
    
    #Bottom right
    window_coords["x2"]=x+w    
    window_coords["y2"]=y+h    
    
    #convert coordinates such that box is shown with respect to crop origin
    annotations["window_xmin"] = annotations["origin_xmin"]- window_coords["x1"]
    annotations["window_ymin"] = annotations["origin_ymin"]- window_coords["y1"]
    annotations["window_xmax"] = annotations["origin_xmax"]- window_coords["x1"]
    annotations["window_ymax"] = annotations["origin_ymax"]- window_coords["y1"]

    #Quickly subset a reasonable set of annotations based on sliding window
    d=annotations[ 
        (annotations["rgb_path"]==image.split("/")[-1]) &
        (annotations.window_xmin > -offset) &  
        (annotations.window_ymin > -offset)  &
        (annotations.window_xmax < (patch_size+ offset)) &
        (annotations.window_ymax < (patch_size+ offset))
                     ]
    
    overlapping_boxes=d[d.apply(box_overlap,window=window_coords,axis=1) > 0.5]
    
    #If boxes fall off edge, clip to window extent    
    overlapping_boxes.loc[overlapping_boxes["window_xmin"] < 0,"window_xmin"]=0
    overlapping_boxes.loc[overlapping_boxes["window_ymin"] < 0,"window_ymin"]=0
    
    #The max size depends on the sliding window
    max_height=window_coords['y2']-window_coords['y1']
    max_width=window_coords['x2']-window_coords['x1']
    
    overlapping_boxes.loc[overlapping_boxes["window_xmax"] > max_width,"window_xmax"]=max_width
    overlapping_boxes.loc[overlapping_boxes["window_ymax"] > max_height,"window_ymax"]=max_height
    
    #format
    boxes=overlapping_boxes[["window_xmin","window_ymin","window_xmax","window_ymax","numeric_label"]].values
    
    print(boxes)
    return(boxes)    


def box_overlap(row,window):
    """
    Calculate the Intersection over Union (IoU) of two bounding boxes.

    Parameters
    ----------
    window : dict
        Keys: {'x1', 'x2', 'y1', 'y2'}
        The (x1, y1) position is at the top left corner,
        the (x2, y2) position is at the bottom right corner
    box : dict
        Keys: {'x1', 'x2', 'y1', 'y2'}
        The (x, y) position is at the top left corner,
        the (x2, y2) position is at the bottom right corner

    Returns
    -------
    float
        in [0, 1]
    """
    
    #construct box
    box={}

    #top left
    box["x1"]=row["origin_xmin"]
    box["y1"]=row["origin_ymin"]

    #Bottom right
    box["x2"]=row["origin_xmax"]
    box["y2"]=row["origin_ymax"]     
    
    assert window['x1'] < window['x2']
    assert window['y1'] < window['y2']
    assert box['x1'] < box['x2']
    assert box['y1'] < box['y2']

    # determine the coordinates of the intersection rectangle
    x_left = max(window['x1'], box['x1'])
    y_top = max(window['y1'], box['y1'])
    x_right = min(window['x2'], box['x2'])
    y_bottom = min(window['y2'], box['y2'])

    if x_right < x_left or y_bottom < y_top:
        return 0.0

    # The intersection of two axis-aligned bounding boxes is always an
    # axis-aligned bounding box
    intersection_area = (x_right - x_left) * (y_bottom - y_top)

    # compute the area of both AABBs
    window_area = (window['x2'] - window['x1']) * (window['y2'] - window['y1'])
    box_area = (box['x2'] - box['x1']) * (box['y2'] - box['y1'])

    overlap = intersection_area / float(box_area)
    return overlap


class OnTheFlyGenerator(Generator):
    """ Generate data for a custom CSV dataset.

    See https://github.com/fizyr/keras-retinanet#csv-datasets for more information.
    """

    def __init__(
        self,
        csv_data_file,
        config,
        base_dir=None,
        **kwargs
    ):
        """ Initialize a CSV data generator.

        Args
            csv_data_file: Path to the CSV annotations file.
            csv_class_file: Path to the CSV classes file.
            base_dir: Directory w.r.t. where the files are to be searched (defaults to the directory containing the csv_data_file).
        """
        self.image_names = []
        self.image_data  = {}
        self.base_dir    = base_dir
        
        #Store config and resolution
        self.config=config
        self.rgb_tile_dir=base_dir
        self.rgb_res=config['rgb_res']
        
        #Holder for image path, keep from reloading same image to save time.
        self.previous_image_path=None
        
        #Holder for previous annotations, after epoch > 1
        self.annotation_dict={}
        
        #debug - plot images, based on config fiile
        self.plot_image=config['plot_image']
        
        # Take base_dir from annotations file if not explicitly specified.
        if self.base_dir is None:
            self.base_dir = os.path.dirname(csv_data_file)
        
        #Read annotations into pandas dataframe
        self.annotation_list=load_csv(csv_data_file, self.rgb_res)
            
        #Compute sliding windows, assumed that all objects are the same extent and resolution
        self.windows=compute_windows(base_dir + self.annotation_list.rgb_path.unique()[0], config["patch_size"], config["patch_overlap"])
        
        #Read classes
        self.classes=_read_classes(data=self.annotation_list)  
        
        #Create label dict
        self.labels = {}
        for key, value in self.classes.items():
            self.labels[value] = key        
        
        #Create list of sliding windows to select
        self.image_data=_read_annotations(self.annotation_list,self.base_dir,self.windows,self.config)
        self.image_names = list(self.image_data.keys())
        
        super(OnTheFlyGenerator, self).__init__(**kwargs)
          
    def show(self,image,boxes):
                
        fig,ax = pyplot.subplots(1)
        ax.imshow(image)
        
        for box in boxes:            
            bottom_left=(box[0],box[1])
            height=box[3]-box[1]
            width=box[2]-box[0]
            rect = patches.Rectangle(bottom_left,width,height,linewidth=1,edgecolor='r',facecolor='none')
            ax.add_patch(rect)
        pyplot.show()
        
    def size(self):
        """ Size of the dataset.
        """
        return len(self.image_names)

    def num_classes(self):
        """ Number of classes in the dataset.
        """
        return max(self.classes.values()) + 1

    def name_to_label(self, name):
        """ Map name to label.
        """
        return self.classes[name]

    def label_to_name(self, label):
        """ Map label to name.
        """
        return self.labels[label]
    
    def image_aspect_ratio(self, image_index):
        """ Compute the aspect ratio for an image with image_index.
        """
        # PIL is fast for metadata
        image = Image.open(self.image_path(image_index))
        return float(image.width) / float(image.height)

    def load_image(self, image_index):
        """ Load an image at the image_index.
        
        """
        
        #Select sliding window and tile
        image_name=self.image_names[image_index]        
        row=self.image_data[image_name]
                
        #Open image to crop
        ##Check if image the is same as previous draw from generator, this will save time.
        if not row["image"] == self.previous_image_path:
            print("Loading new tile: %s" %(row["image"]))
            im = Image.open(self.base_dir+row["image"])
            self.numpy_image = np.array(im)    
        
        #Load rgb image and get crop
        image=retrieve_window(numpy_image=self.numpy_image,index=row["windows"],windows=self.windows)
        
        #Store if needed for show, in RGB color space.
        self.image=image
        
        #BGR order
        image=image[:,:,::-1].copy()
        
        #Save image path for next evaluation to check
        self.previous_image_path = row["image"]
        
        return image

    def load_annotations(self, image_index):
        """ Load annotations for an image_index.
        """
        
        #Find the original data and crop
        image_name=self.image_names[image_index]
        row=self.image_data[image_name]
        
        #Look for annotations in previous epoch
        key=row["image"]+"_"+str(row["windows"])
        
        if key in self.annotation_dict:
            boxes=self.annotation_dict[key]
        else:
            #Which annotations fall into that crop?
            self.annotation_dict[key]=fetch_annotations(image=self.base_dir+row["image"],
                                           index=row["windows"],
                                           annotations=self.annotation_list,
                                           windows=self.windows,
                                           offset=(self.config["patch_size"] * 0.1)/self.rgb_res,
                                           patch_size=self.config["patch_size"])

        #Index
        boxes=np.copy(self.annotation_dict[key])

        #view image if needed on debug
        if self.plot_image:
            self.show(self.image,boxes)
         
        return boxes
    
