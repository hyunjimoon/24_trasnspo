import traci
import numpy as np
import pandas as pd
import random
import timeit
import torch
import torch.optim as optim
from torch.autograd import Variable

DEVICE = torch.device("mps" if torch.backends.mps.is_available() else "cuda:0" if torch.cuda.is_available() else "cpu")
print('Device', DEVICE)

# phase codes based on environment.net.xml
PHASE_NS_GREEN = 0  # action 0 code 00
PHASE_NS_YELLOW = 1
PHASE_NSL_GREEN = 2  # action 1 code 01
PHASE_NSL_YELLOW = 3
PHASE_EW_GREEN = 4  # action 2 code 10
PHASE_EW_YELLOW = 5
PHASE_EWL_GREEN = 6  # action 3 code 11
PHASE_EWL_YELLOW = 7


class Simulation:
    def __init__(self, Model, ReplayBuffer, TrafficGen, sumo_cmd, gamma, max_steps, green_duration,
                 yellow_duration, num_states, num_actions, training_epochs, batch_size, learning_rate, num_lanes, lane_length,reward_type):
        self.sum_waiting_time = 0
        self.sum_queue_length = 0
        self.waiting_times = {}
        self.average_speed = {}
        self.dqn = Model
        self.traffic_gen = TrafficGen
        self.gamma = gamma
        self.step = 0
        self.sumo_cmd = sumo_cmd
        self.max_steps = max_steps
        self.green_duration = green_duration
        self.yellow_duration = yellow_duration
        self.num_states = num_states
        self.num_actions = num_actions
        self.training_epochs = training_epochs
        self.num_lanes = num_lanes
        self.lane_length = lane_length
        self.replay_buffer = ReplayBuffer
        self.optimizer = optim.Adam(self.dqn.parameters(), lr=learning_rate)
        self.batch_size = batch_size
        self.reward_type = reward_type

    def run(self, episode, epsilon):
        """
        Runs an episode of simulation, then starts a training session
        """
        start_time = timeit.default_timer()

        # generate the route file for this simulation and set up sumo
        self.traffic_gen.generate_netfile(seed=episode)
        self.traffic_gen.generate_routefile(seed=episode)
        traci.start(self.sumo_cmd)

        print("\t [INFO] Start simulating the episode")

        # inits
        self.step = 0
        old_total_wait = 0
        old_average_speed = 0
        old_state = -1
        old_action = -1
        
        sum_reward = 0
        sum_waiting = 0
        sum_average_speed = 0
        sum_current_veh = 0
        sum_passed_veh = 0
        
        while self.step < self.max_steps:

            # get current state of the intersection
            current_state = self.get_state()

            # calculate reward of previous action: (change in cumulative waiting time between actions)
            # waiting time = seconds waited by a car since the spawned in the environment,
            current_total_wait = self.collect_waiting_times()
            average_speed = self.collect_average_speed()
            current_veh = self.collect_current_waiting_vehicle_count()
            passed_veh = self.collect_current_passed_vehicle_count()
            
            # reward definition: change in cumulative waiting time between actions
            # Base reward is the change in total waiting time
            # Only record the useful reward at the last step. You are not expected to converge given 
            # this reward function.
            # HINT: Specify a reward only at the end of the episode. However, this MDP advances
            # multiple up to 14 time steps at a time (4 sec yellow + 10 sec new phase), so be careful 
            # about how you trigger the sparse reward.
            
            #DONE: reward became less sparse
            base_reward = -current_total_wait     
            if self.reward_type == 'base':
                reward = base_reward
            # TODO:Add different types of reward options: speed, speed change,sparse, custom
            elif self.reward_type == 'waittime':            
                # Waiting time change as the reward. No more additional reward needed for
                reward = old_total_wait - current_total_wait
            elif self.reward_type == 'speed':
                #TODO: experiment reward = i) average_speed ii) old_average_speed - average_speed iii)average_speed/self.training_epochs
                tuning_reward = average_speed
                reward = base_reward + tuning_reward
                # reward = average_speed #old_average_speed - average_speed
                tuning_reward = 0 # TODO: Average speed reward tuning
                reward = base_reward + tuning_reward
            elif self.reward_type == 'custom':
                #DONE: reward shaping
                state_potential = self.get_potential(old_state)
                next_state_potential = self.get_potential(current_state)
                potential = self.gamma * next_state_potential - state_potential
                reward = base_reward + potential
                pass
            else:
                raise ValueError("Reward type not recognized")

            # saving the data into the memory
            if self.step != 0:
                self.replay_buffer.push(old_state, old_action, reward, current_state)

            # choose the light phase to activate, based on the current state of the intersection
            action = self.choose_action(current_state, epsilon)

            # if the chosen phase is different from the last phase, activate the yellow phase
            if self.step != 0 and old_action != action:
                self.set_yellow_phase(old_action)
                self.simulate(self.yellow_duration)

            # execute the phase selected before
            self.set_green_phase(action)
            self.simulate(self.green_duration)

            # saving variables for later & accumulate reward
            old_state = current_state
            old_action = action
            old_total_wait = current_total_wait
            old_average_speed = average_speed

            # saving only the meaningful reward to better see if the agent is behaving correctly
            # if reward < 0:
                # sum_reward += reward
            sum_reward += reward
            
            sum_waiting += current_total_wait
            sum_average_speed += average_speed
            sum_current_veh += current_veh  
            sum_passed_veh += passed_veh

        avg_reward = sum_reward / self.max_steps
        avg_waiting = sum_waiting / self.max_steps
        avg_average_speed = sum_average_speed / self.max_steps
        avg_current_veh = sum_current_veh / self.max_steps
        avg_passed_veh = sum_passed_veh / self.max_steps
        
        traci.close()
        simulation_time = round(timeit.default_timer() - start_time, 1)

        print("\t [STAT] Average reward:", avg_reward,
              "Average waiting time:", avg_waiting,
              "Average speed:", avg_average_speed,
              "Average current vehicles:", avg_current_veh,
              "Average passed vehicles:", avg_passed_veh,
              "- Epsilon:", round(epsilon, 2))

        print("\t [INFO] Training the DQN")
        start_time = timeit.default_timer()
        # training the DQN

        sum_training_loss = 0
        for _ in range(self.training_epochs):
            sum_training_loss += self.compute_td_loss()
        avg_training_loss = sum_training_loss.item() / self.max_steps
        print("\t [STAT] Training Loss :", avg_training_loss)
        training_time = round(timeit.default_timer() - start_time, 1)

        return simulation_time, training_time, avg_reward, avg_waiting, avg_training_loss, avg_average_speed, avg_current_veh, avg_passed_veh
    
    
    def simulate(self, steps_todo):
        """
        Execute steps in sumo while gathering statistics
        """
        # do not do more steps than the maximum allowed number of steps
        if (self.step + steps_todo) >= self.max_steps:
            steps_todo = self.max_steps - self.step

        while steps_todo > 0:
            traci.simulationStep()  # simulate 1 step in sumo
            self.step += 1  # update the step counter
            steps_todo -= 1
            queue_length = self.get_queue_length()
            self.sum_queue_length += queue_length
            # 1 step while waiting in queue means 1 second waited
            # for each car, therefore queue_length == waited_seconds
            self.sum_waiting_time += queue_length
            
    def collect_average_speed(self):
        """
        Retrieve the average speed of all cars in the incoming roads
        """
        car_list = traci.vehicle.getIDList()
        for car_id in car_list:
            speed = traci.vehicle.getSpeed(car_id)
            self.average_speed[car_id] = speed
        average_speed = sum(self.average_speed.values()) / len(self.average_speed) if len(self.average_speed) > 0 else 0
        return average_speed
    
    def collect_current_waiting_vehicle_count(self):
        """
        Retrieve the number of cars in the incoming roads
        """
        vehicle_count = 0
        for road in ["E2TL", "N2TL", "W2TL", "S2TL"]:
            vehicle_count += traci.edge.getLastStepVehicleNumber(road)
        return vehicle_count

    def collect_current_passed_vehicle_count(self):
        """
        Retrieve the number of cars in the incoming roads
        """
        vehicle_count = 0
        for road in ["TL2E", "TL2N", "TL2W", "TL2S"]:
            vehicle_count += traci.edge.getLastStepVehicleNumber(road)
        return vehicle_count

    def collect_waiting_times(self):
        """
        Retrieve the waiting time of every car in the incoming roads and return the total waiting time
        """
        incoming_roads = ["E2TL", "N2TL", "W2TL", "S2TL"]
        car_list = traci.vehicle.getIDList()
        for car_id in car_list:
            wait_time = traci.vehicle.getAccumulatedWaitingTime(car_id)
            # get the road id where the car is located
            road_id = traci.vehicle.getRoadID(car_id)
            # consider only the waiting times of cars in incoming roads
            if road_id in incoming_roads:
                self.waiting_times[car_id] = wait_time
            else:
                # a car that was tracked has cleared the intersection
                if car_id in self.waiting_times:
                    del self.waiting_times[car_id]
        total_waiting_time = sum(self.waiting_times.values())
        return total_waiting_time

    def choose_action(self, state, epsilon):
        """
        According to epsilon-greedy policy, decide whether to perform exploration or exploitation
        """
        if random.random() < epsilon:
            # random action
            return random.randint(0, self.num_actions - 1)
        else:
            # the best action given the current state
            state = Variable(torch.tensor(state, dtype=torch.float32, device=DEVICE).unsqueeze(0), requires_grad=False)
            q_value = self.dqn.forward(state)
            return q_value.max(1)[1].data[0]

    def set_yellow_phase(self, old_action):
        """
        Activate the correct yellow light combination in sumo
        """
        # obtain the yellow phase code, based on the old action (ref on environment.net.xml)
        yellow_phase_code = old_action * 2 + 1
        traci.trafficlight.setPhase("TL", yellow_phase_code)

    def set_green_phase(self, action_number):
        """
        Activate the correct green light combination in sumo
        """
        if action_number == 0:
            traci.trafficlight.setPhase("TL", PHASE_NS_GREEN)
        elif action_number == 1:
            traci.trafficlight.setPhase("TL", PHASE_NSL_GREEN)
        elif action_number == 2:
            traci.trafficlight.setPhase("TL", PHASE_EW_GREEN)
        elif action_number == 3:
            traci.trafficlight.setPhase("TL", PHASE_EWL_GREEN)

    def get_queue_length(self):
        """
        Retrieve the number of cars with speed = 0 in every incoming lane
        """
        halt_N = traci.edge.getLastStepHaltingNumber("N2TL")
        halt_S = traci.edge.getLastStepHaltingNumber("S2TL")
        halt_E = traci.edge.getLastStepHaltingNumber("E2TL")
        halt_W = traci.edge.getLastStepHaltingNumber("W2TL")
        queue_length = halt_N + halt_S + halt_E + halt_W
        return queue_length

    def get_state(self):
        """
        Retrieve the state of the intersection from sumo, in the form of cell occupancy
        """
        state = np.zeros(self.num_states)
        car_list = traci.vehicle.getIDList()
        num_lanes = self.num_lanes

        for car_id in car_list:
            lane_pos = traci.vehicle.getLanePosition(car_id)
            lane_id = traci.vehicle.getLaneID(car_id)
            # invert lane position value,
            # so if the car is close to the traffic light -> lane_pos = 0 -> 750 = max len of a road
            # https://sumo.dlr.de/pydoc/traci._vehicle.html#VehicleDomain-getLanePosition
            lane_pos = self.lane_length - lane_pos

            # distance in meters from the traffic light -> mapping into cells
            if lane_pos < self.lane_length/750*7:
                lane_cell = 0
            elif lane_pos < self.lane_length/750*14:
                lane_cell = 1
            elif lane_pos < self.lane_length/750*21:
                lane_cell = 2
            elif lane_pos < self.lane_length/750*28:
                lane_cell = 3
            elif lane_pos < self.lane_length/750*40:
                lane_cell = 4
            elif lane_pos < self.lane_length/750*60:
                lane_cell = 5
            elif lane_pos < self.lane_length/750*100:
                lane_cell = 6
            elif lane_pos < self.lane_length/750*160:
                lane_cell = 7
            elif lane_pos < self.lane_length/750*400:
                lane_cell = 8
            elif lane_pos <= self.lane_length:
                lane_cell = 9

            # finding the lane where the car is located 
            # x2TL_3 are the "turn left only" lanes
            # if lane_id == "W2TL_0" or lane_id == "W2TL_1" or lane_id == "W2TL_2":
            #     lane_group = 0
            # elif lane_id == "W2TL_3":
            #     lane_group = 1
            # elif lane_id == "N2TL_0" or lane_id == "N2TL_1" or lane_id == "N2TL_2":
            #     lane_group = 2
            # elif lane_id == "N2TL_3":
            #     lane_group = 3
            # elif lane_id == "E2TL_0" or lane_id == "E2TL_1" or lane_id == "E2TL_2":
            #     lane_group = 4
            # elif lane_id == "E2TL_3":
            #     lane_group = 5
            # elif lane_id == "S2TL_0" or lane_id == "S2TL_1" or lane_id == "S2TL_2":
            #     lane_group = 6
            # elif lane_id == "S2TL_3":
            #     lane_group = 7
            # else:
            #     lane_group = -1
                
            if num_lanes==1:
                lane_group = 0
            else:
                direction, lane_number = lane_id.split('_')[0][0], int(lane_id.split('_')[-1])
                direction_base = {"W": 0, "N": 1, "E": 2, "S": 3}
                if direction in direction_base:
                    # Calculate base lane group number for this direction
                    base_group = direction_base[direction] * 2
                    
                    if lane_number == num_lanes - 1:
                        # This is the left-turn only lane
                        lane_group = base_group + 1
                    else:
                        # Other lanes
                        lane_group = base_group
                else:
                    # If the lane_id does not match expected format, assign an error group
                    lane_group = -1

            if 1 <= lane_group <= 7:
                # composition of the two position ID to create a number in interval 0-79
                car_position = int(str(lane_group) + str(lane_cell))
                valid_car = True
            elif lane_group == 0:
                car_position = lane_cell
                valid_car = True
            else:
                # flag for not detecting cars crossing the intersection or driving away from it
                valid_car = False

            if valid_car:
                # write the position of the car car_id in the state array in the form of "cell occupied"
                state[car_position] = 1

        return state

    def compute_td_loss(self):

        """
        Compute the temporal difference loss as defined by the Q update
        """

        # sample a batch from the replay buffer
        state, action, reward, next_state = self.replay_buffer.sample(self.batch_size)

        # convert to pytorch variables
        state = Variable(torch.tensor(state, dtype=torch.float32, device=DEVICE), requires_grad=False) #.to(DEVICE)
        next_state = Variable(torch.tensor(next_state, dtype=torch.float32, device=DEVICE), requires_grad=False) #.to(DEVICE)
        action = Variable(torch.tensor(action, dtype=torch.int64, device=DEVICE), requires_grad=False) #.to(DEVICE)
        reward = Variable(torch.tensor(reward, dtype=torch.float32, device=DEVICE), requires_grad=False) #.to(DEVICE)

        # obtain the q value for the current state by feeding the state to the DQN
        q_values = self.dqn.forward(state)
        # obtain the q value for the next state by feeding the next state to the DQN
        next_q_values = self.dqn.forward(next_state)

        q_value = q_values.gather(1, action.unsqueeze(1)).squeeze(1)
        next_q_value = next_q_values.max(1)[0]

        # TODO : Calculate the loss
        # set the loss variable to the right expression using the computed quantities above
        expected_q_value = reward + self.gamma * next_q_value
        loss = torch.nn.functional.mse_loss(q_value, expected_q_value.detach())

        # next we do the gradient update based on the calculated loss
        self.optimizer.zero_grad()
        loss.backward() # PyTorch will automatically do the gradient update on the defined loss 
        self.optimizer.step()

        return loss

    def save_model(self, path, episode):
        """
        Save the DQN model
        """
        torch.save(self.dqn, path + "/" + str(episode))

    def get_potential(self, state):
        if state is not None:
            # Calculate the potential based on the state
            # Example: potential is the negative of the total waiting time
            potential = -self.collect_waiting_times()
        else:
            potential = 0.0
        return potential