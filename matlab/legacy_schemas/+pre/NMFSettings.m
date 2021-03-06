%{
pre.NMFSettings (lookup) # Settings for the NMF algorithm
->pre.SegmentMethod
setting_idx            : tinyint      # unique index for the configuration
-----
name                   : char(20)     # descriptive name of the settings
p=2                    : int          # initial order of autoregressive system (p = 0 no dynamics, p=1 just decay, p = 2, both rise and decay)
merge_thr=.8           : double       # merging threshold
max_iter=2             : int          # maximum number of iterations
tau = 4                : double       # std of gaussian kernel (size of neuron)
fudge_factor=0.98      : double       # bias correction for AR coefficients
temporal_iter=2        : int          # number of block-coordinate descent steps
deconv_method          : varchar(100) # activity deconvolution method
search_method          : varchar(100) # param for updating spatial components
dist                   : int          # param for updating spatial components
density                : double       # neurons per slice per mm^2
se                     : int          # morphological element for dilation passed to strel
downsample_to          : double       # downsample the temporal traces to that sampling rate
%}

classdef NMFSettings < dj.Relvar
    methods
        function prepare(self)

            key = struct(...
                'segment_method', fetch1(pre.SegmentMethod & 'method_name="nmf"', 'segment_method'), ...
                'name', 'less_merging', ...
                'setting_idx', 1, ...
                'search_method','ellipse','dist',3,...
                'deconv_method','constrained_foopsi',...
                'temporal_iter',2,...
                'fudge_factor',0.98,...
                'merge_thr',0.9,...
                'p', 2, ...
                'max_iter', 2, ...
                'density', 800, ...
                'se', 3 , ... 
                'downsample_to', 4  ...
                );
            insert(self, key)
            key = struct(...
                'segment_method', fetch1(pre.SegmentMethod & 'method_name="nmf"', 'segment_method'), ...
                'name', 'more_iterations', ...
                'setting_idx', 2, ...
                'search_method','ellipse','dist',3,...
                'deconv_method','constrained_foopsi',...
                'temporal_iter',2,...
                'fudge_factor',0.98,...
                'merge_thr',0.9,...
                'p', 2, ...
                'max_iter', 5, ...
                'density', 800, ...
                'se', 3 , ... 
                'downsample_to', 5  ...
                );
            insert(self, key)
            key = struct(...
                'segment_method', fetch1(pre.SegmentMethod & 'method_name="nmf"', 'segment_method'), ...
                'name', 'default', ...
                'setting_idx', 3, ...
                'search_method','ellipse','dist',3,...
                'deconv_method','constrained_foopsi',...
                'temporal_iter',2,...
                'fudge_factor',0.98,...
                'merge_thr',0.8,...
                'p', 2, ...
                'max_iter', 2, ...
                'density', 800, ...
                'se', 3 , ... 
                'downsample_to', 4  ...
                );
            insert(self, key)
            
        end
        
    end
    
end