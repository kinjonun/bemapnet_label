from pyquaternion import Quaternion
import numpy as np


# Token   80d56e801c7e465995bdb116b3e678aa
# lidar_ego2global_r = [-0.7826393592967994, 0.00867598472714821, -0.010566539708221056, 0.6223252435881937]
# lidar_ego2global_t = [132.0368788137383, 997.2431802967292, 0.0]
# lidar2ego_t = [0.943713, 0.0, 1.84023]
# lidar2ego_r = [0.7077955119163518, -0.006492242056004365, 0.010646214713995808, -0.7063073142877817]
# # CAM_FRONT
# cam_ego2global_r = [-0.7826162378208529, 0.008463341932761203, -0.01055200652286372, 0.6223574947735464]
# cam_ego2global_t = [131.98652774136764, 997.4686079778229, 0.0]
# cam2ego_t = [1.70079118954, 0.0159456324149, 1.51095763913]
# cam2ego_r = [0.4998015430569128, -0.5030316162024876, 0.4997798114386805, -0.49737083824542755]

# Token   a4d6e99f30ed4c5589bf6f87503fa064
lidar_ego2global_r = [0.9407530460837824, -0.0030850369357938584, 0.0030934501402690006, -0.3390643292907983]
lidar_ego2global_t = [1366.5012706928185, 2671.734949493252, 0.0]
lidar2ego_t = [0.943713, 0.0, 1.84023]
lidar2ego_r = [0.7077955119163518, -0.006492242056004365, 0.010646214713995808, -0.7063073142877817]
# CAM_FRONT
cam_ego2global_r = [0.9407640518280307, -0.0033290920185497005, 0.0035104642333263917, -0.33902742156838184]
cam_ego2global_t = [1366.2753023605494, 2671.925415872204, 0.0]
cam2ego_t = [1.70079118954, 0.0159456324149, 1.51095763913]
cam2ego_r = [0.4998015430569128, -0.5030316162024876, 0.4997798114386805, -0.49737083824542755]

lidar2global_r = Quaternion(lidar_ego2global_r).rotation_matrix @ Quaternion(lidar2ego_r).rotation_matrix
cam2global_r = Quaternion(cam_ego2global_r).rotation_matrix @ Quaternion(cam2ego_r).rotation_matrix

lidar2global_t = lidar_ego2global_t + Quaternion(lidar_ego2global_r).rotation_matrix @ np.array(lidar2ego_t)
cam2global_t = cam_ego2global_t + Quaternion(cam_ego2global_r).rotation_matrix @ np.array(cam2ego_t)

cam2lidar_r = np.linalg.inv(lidar2global_r) @ cam2global_r
# [[ 0.99997343  0.00637306 -0.00353914] [ 0.00342167  0.01834994  0.99982577] [ 0.00643689 -0.99981131  0.01832765]]
lidar2cam_r = np.linalg.inv(cam2global_r) @ lidar2global_r
# [[ 0.99997343  0.00342167  0.00643689] [ 0.00637306  0.01834994 -0.99981131] [-0.00353914  0.99982577  0.01832765]]

lidar2cam_t = cam2global_r.T @ (lidar2global_t - cam2global_t)
# [ 0.01422762 -0.3278505  -0.52849908]

print(lidar2cam_t)
print(lidar2cam_r)