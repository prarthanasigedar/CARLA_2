#!/usr/bin/env python

# Copyright (c) 2018 Intel Labs.
# authors: German Ros (german.ros@intel.com)
#
# This work is licensed under the terms of the MIT license.
# For a copy, see <https://opensource.org/licenses/MIT>.

""" This module contains a local planner to perform
low-level waypoint following based on PID controllers. """

from collections import deque
from enum import Enum
import numpy as np
import math
import cv2
import matplotlib.pyplot as plt

import carla
from agents.navigation.controller import VehiclePIDController
from agents.tools.misc import distance_vehicle, draw_waypoints
from agents.navigation.rrt_grid import RRT




class RoadOption(Enum):
    """
    RoadOption represents the possible topological configurations
    when moving from a segment of lane to other.
    """
    VOID = -1
    LEFT = 1
    RIGHT = 2
    STRAIGHT = 3
    LANEFOLLOW = 4
    CHANGELANELEFT = 5
    CHANGELANERIGHT = 6


class LocalPlanner(object):
    """
    LocalPlanner implements the basic behavior of following a trajectory
    of waypoints that is generated on-the-fly.
    The low-level motion of the vehicle is computed by using two PID controllers,
    one is used for the lateral control
    and the other for the longitudinal control (cruise speed).

    When multiple paths are available (intersections)
    this local planner makes a random choice.
    """

    # Minimum distance to target waypoint as a percentage
    # (e.g. within 80% of total distance)

    # FPS used for dt
    FPS = 20

    def __init__(self, agent):
        """
        :param agent: agent that regulates the vehicle
        :param vehicle: actor to apply to local planner logic onto
        """
        self._vehicle = agent.vehicle
        self._map = agent.vehicle.get_world().get_map()

        self._target_speed = None
        self.sampling_radius = None
        self._min_distance = None
        self._current_waypoint = None
        self.target_road_option = None
        self._next_waypoints = None
        self.target_waypoint = None
        self._vehicle_controller = None
        self._global_plan = None
        self._pid_controller = None
        self.waypoints_queue = deque(maxlen=20000)  # queue with tuples of (waypoint, RoadOption)
        self._buffer_size = 5
        self._waypoint_buffer = deque(maxlen=self._buffer_size)
        self.rrt_buffer = deque(maxlen=10000)

        self.cw_x   =     None
        self.cy_y   =     None
        self.tw_x   =     None
        self.tw_y   =     None
        self._dist  =     None
        self._alpha =     None
        self._b     =     None
        self._a     =     None
        self.path   =     None



        self._init_controller()  # initializing controller

    def reset_vehicle(self):
        """Reset the ego-vehicle"""
        self._vehicle = None
        print("Resetting ego-vehicle!")

    def _init_controller(self):
        """
        Controller initialization.

        dt -- time difference between physics control in seconds.
        This is can be fixed from server side
        using the arguments -benchmark -fps=F, since dt = 1/F

        target_speed -- desired cruise speed in km/h

        min_distance -- minimum distance to remove waypoint from queue

        lateral_dict -- dictionary of arguments to setup the lateral PID controller
                            {'K_P':, 'K_D':, 'K_I':, 'dt'}

        longitudinal_dict -- dictionary of arguments to setup the longitudinal PID controller
                            {'K_P':, 'K_D':, 'K_I':, 'dt'}
        """
        # Default parameters
        self.args_lat_hw_dict = {
            'K_P': 0.75,
            'K_D': 0.02,
            'K_I': 0.4,
            'dt': 1.0 / self.FPS}
        self.args_lat_city_dict = {
            'K_P': 0.58,
            'K_D': 0.02,
            'K_I': 0.5,
            'dt': 1.0 / self.FPS}
        self.args_long_hw_dict = {
            'K_P': 0.37,
            'K_D': 0.024,
            'K_I': 0.032,
            'dt': 1.0 / self.FPS}
        self.args_long_city_dict = {
            'K_P': 0.15,
            'K_D': 0.05,
            'K_I': 0.07,
            'dt': 1.0 / self.FPS}

        self._current_waypoint = self._map.get_waypoint(self._vehicle.get_location())

        self._global_plan = False

        self._target_speed = self._vehicle.get_speed_limit()

        self._min_distance = 3

    def set_speed(self, speed):
        """
        Request new target speed.

            :param speed: new target speed in km/h
        """

        self._target_speed = speed

    def set_global_plan(self, current_plan, clean=False):
        """
        Sets new global plan.

            :param current_plan: list of waypoints in the actual plan
        """
        for elem in current_plan:
            self.waypoints_queue.append(elem)

        if clean:
            self._waypoint_buffer.clear()
            for _ in range(self._buffer_size):
                if self.waypoints_queue:
                    self._waypoint_buffer.append(
                        self.waypoints_queue.popleft())
                else:
                    break

        self._global_plan = True

    def get_incoming_waypoint_and_direction(self, steps=3):
        """
        Returns direction and waypoint at a distance ahead defined by the user.

            :param steps: number of steps to get the incoming waypoint.
        """
        if len(self.waypoints_queue) > steps:
            return self.waypoints_queue[steps]

        else:
            try:
                wpt, direction = self.waypoints_queue[-1]
                return wpt, direction
            except IndexError as i:
                print(i)
                return None, RoadOption.VOID
        return None, RoadOption.VOID
    
    def occupancy_grid(self,img): 
          
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        img = np.asarray(img)
        #print(np.unique(img, return_counts=True))
        pixel_value, pixel_freq = np.unique(img, return_counts=True)
        vehicle_pixels = [pixel_value[i] for i in range (len(pixel_value)) if pixel_freq[i]<500]
        vehicle_color = 164 #pixel value of the spawned vehicle in the BEV image
        #start_pos = np.where(img == vehicle_color)
        #print("start position is " , start_pos)
        #centre_pos = np.asarray(((start_pos[0][0] + start_pos[0][-1])/2, (start_pos[-1][0] + start_pos[-1][-1])/2), dtype=np.int32)
        #print("vehicle_centre is ", centre_pos)
        
        #print("image shape is ", np.shape(img))
        grid = np.ones((img.shape[0], img.shape[1]))
        #print('grid shape is ', grid.shape)
        grid[img == 0] = 0
        grid[img == 150] = 0

        for i in vehicle_pixels: # for pedestrians and other small moving objects
            grid[img == i] = 0
        
        grid[np.where(img == vehicle_color)]= 0.5
        #cv2.imshow("Grid", grid)
        return grid
    

    def pixel_to_world(self,a,b):
        dx = abs(int(a)-75)
        d = math.sqrt((int(a)-75)**2 + (int(b)-168)**2)
        print(dx)
        print(d)
        alpha = math.asin(dx/d)

        gamma = math.radians(self.cw_yaw) + alpha  # vehicle angle + alpha
        d = d/4 # in pixel per metre

        l_x = d * math.sin(gamma)
        l_y = d * math.cos(gamma)

        l = carla.Location(x= self.cw_x  + l_x, y= self.cw_y  + l_y)

        return l
        

    def run_step(self, target_speed=None,rgb=None, debug=True):
        """
        Execute one step of local planning which involves
        running the longitudinal and lateral PID controllers to
        follow the waypoints trajectory.

            :param target_speed: desired speed
            :param debug: boolean flag to activate waypoints debugging
            :return: control
        """

        if target_speed is not None:
            self._target_speed = target_speed
        else:
            self._target_speed = self._vehicle.get_speed_limit()

        if len(self.waypoints_queue) == 0:
            control = carla.VehicleControl()
            control.steer = 0.0
            control.throttle = 0.0
            control.brake = 1.0
            control.hand_brake = False
            control.manual_gear_shift = False
            return control

        # Buffering the waypoints
        if not self._waypoint_buffer:
            for i in range(self._buffer_size):

                if self.waypoints_queue:
                    for i in range(4):
                        #print(self.waypoints_queue[0])
                        self.waypoints_queue.popleft()
                    self._waypoint_buffer.append(
                        self.waypoints_queue.popleft())
                    print(self._waypoint_buffer)
                else:
                    break
        

        # Current vehicle waypoint
        self._current_waypoint = self._map.get_waypoint(self._vehicle.get_location())
        #getting the cordinates of current vehicle location
        self.cw_x = self._current_waypoint.transform.location.x
        self.cw_y = self._current_waypoint.transform.location.y
        self.cw_yaw = self._current_waypoint.transform.rotation.yaw
        print( "x and y of current wap", self.cw_x,self.cw_y)
        print("current yaw", self.cw_yaw)
    
        # Target waypoint
        print("waypoint buffer value", self._waypoint_buffer[0])
        self.target_waypoint, self.target_road_option = self._waypoint_buffer[0]
        #getting the cordinates of target vehicle location
        self.tw_x = self.target_waypoint.transform.location.x
        self.tw_y = self.target_waypoint.transform.location.y
        self.tw_yaw = self.target_waypoint.transform.rotation.yaw
        print("target yaw", self.tw_yaw)
        print( "x and y of target wap", self.tw_x,self.tw_y)

        self._dist = math.sqrt((self.cw_x - self.tw_x)**2 + (self.cw_y - self.tw_y)**2)
        print("hypotenuse is", self._dist)
        self._dist = self._dist * 4 # pixel per metre = 4

        self._alpha = self.cw_yaw - self.tw_yaw # absolute angle
        print("alpha",self._alpha)

        self._a =  self._dist * math.sin(self._alpha)  # finding the height and width according 
        self._b =  self._dist * math.cos(self._alpha)
        print(" a and b are", self._a,self._b)

        self.oc_grid = self.occupancy_grid(rgb)
        

        self.start_pos = cv2.circle(self.oc_grid, (75,168), 3, (0,255,0), 3)
        self.target_pos = cv2.circle(self.start_pos, (75-int(self._a),168-int(self._b)), 3, (0,0,255), 3)
        #print(75 -int(self._a))
        #print(168-int(self._b))

        
        cv2.imshow("grid", self.target_pos)
        cv2.waitKey(1)
        # end of part 1


        #rrt_buffer
        if not self.rrt_buffer:                  
            if self._waypoint_buffer:
                self.target_waypoint, self.target_road_option = self._waypoint_buffer[0]
                self._waypoint_buffer.popleft()
                print("goal is", 75-int(self._a), 168-int(self._b))
                rrt = RRT(
                    start=[75, 168],
                    goal=[75-int(self._a), 168-int(self._b)],
                    grid = self.oc_grid)

                #print(rrt)


                self.path = rrt.planning(animation= True)
                print("MAIN PATH",self.path)
                self.path = self.path[:-2]
                self.pathss = self.path
                print("excluding path",self.pathss)
                

                for (x,y) in self.path:
                    
                    m = self.pixel_to_world(x,y)
                    m_waypoint = self._map.get_waypoint(m)
                    self.rrt_buffer.appendleft(m_waypoint)
        
        if self.rrt_buffer:
            self.local_target = self.rrt_buffer.popleft()
            print(self.local_target, "local_target")

        # plt.imshow(self.oc_grid, cmap='gray')
        # plt.plot([x for (x, y) in self.path], [y for (x, y) in self.path], '-r')
        # plt.plot(self.cw_x, self.cw_y, "xr")
        # plt.plot(self.tw_x, self.tw_y, "xr")
        # plt.grid(True)
        # plt.axis([0, 336, 0, 150])
        # plt.pause(0.01)  # Need for Mac
        # plt.show()

        if target_speed > 50:
            args_lat = self.args_lat_hw_dict
            args_long = self.args_long_hw_dict
        else:
            args_lat = self.args_lat_city_dict
            args_long = self.args_long_city_dict

        self._pid_controller = VehiclePIDController(self._vehicle,
                                                    args_lateral=args_lat,
                                                    args_longitudinal=args_long)

        control = self._pid_controller.run_step(self._target_speed, self.local_target)

        # Purge the queue of obsolete waypoints
        vehicle_transform = self._vehicle.get_transform()
        #print(vehicle_transform)
        max_index = -1

        for i, (waypoint, _) in enumerate(self._waypoint_buffer):
            if distance_vehicle(
                    waypoint, vehicle_transform) < self._min_distance:
                max_index = i
        if max_index >= 0:
            for i in range(max_index + 1):
                self._waypoint_buffer.popleft()

        if debug:
            draw_waypoints(self._vehicle.get_world(),
                           [self.local_target], 1.0)
        return control