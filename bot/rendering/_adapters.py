from typing import Optional


class MatchShim:
    def __init__(self, match_data: list):
        self.p1_id = match_data[0]
        self.p2_id = match_data[1]
        self.p1_score = match_data[2]
        self.p2_score = match_data[3]
        self.winner_id = match_data[4]
        self.p1_name = match_data[5] if len(match_data) > 5 else ""
        self.p2_name = match_data[6] if len(match_data) > 6 else ""
        self.p1_country = match_data[7] if len(match_data) > 7 else ""
        self.p2_country = match_data[8] if len(match_data) > 8 else ""
        self.match_id = match_data[9] if len(match_data) > 9 else None
        self.p1_prereq = match_data[10] if len(match_data) > 10 else None
        self.p2_prereq = match_data[11] if len(match_data) > 11 else None
        self.bracket_slot = match_data[12] if len(match_data) > 12 else None
        self.series_game = match_data[13] if len(match_data) > 13 else 1

    def is_finished(self) -> bool:
        return bool(self.winner_id)

    def get_players(self) -> list:
        return [self.p1_id, self.p2_id]

    def get_opponent(self, player_id: int):
        if self.p1_id == player_id:
            return PlayerShim(self.p2_id, self.p2_name, self.p2_country)
        return PlayerShim(self.p1_id, self.p1_name, self.p1_country)

    def get_score_from_player(self, player_id: int):
        if player_id == self.p1_id:
            return (self.p1_score, self.p2_score)
        return (self.p2_score, self.p1_score)

    def get_score_from_left(self):
        return (self.p1_score, self.p2_score)

    def get_winner(self):
        if self.winner_id == self.p1_id:
            return PlayerShim(self.p1_id, self.p1_name, self.p1_country)
        elif self.winner_id == self.p2_id:
            return PlayerShim(self.p2_id, self.p2_name, self.p2_country)
        return None

    def get_loser(self):
        if self.winner_id == self.p1_id:
            return PlayerShim(self.p2_id, self.p2_name, self.p2_country)
        elif self.winner_id == self.p2_id:
            return PlayerShim(self.p1_id, self.p1_name, self.p1_country)
        return None


class PlayerShim:
    def __init__(self, id: int, name: str, country: str):
        self.id = id
        self.name = name
        self.country = country or ""

    def __eq__(self, other):
        if isinstance(other, int):
            return self.id == other
        return self.id == other.id

    def get_matches_array(self, match_array: list):
        return [m for m in match_array if self.id in m.get_players()]

    @staticmethod
    def replace_from_array(player_obj: "PlayerShim", player_arr: list["PlayerShim"]):
        for player in player_arr:
            if player == player_obj:
                return player
        player_arr.append(player_obj)
        return player_obj

    @staticmethod
    def get_from_array(player_id: int, player_arr: list["PlayerShim"]):
        for player in player_arr:
            if player.id == player_id:
                return player


class OpenPlayerShim(PlayerShim):
    def __init__(self, id: int, name: str, country: str):
        super().__init__(id, name, country)
        self.wins = 0
        self.losses = 0
        self.gd = 0
        self.goals_scored = 0
        self.defeated = []
        self.best_defeated = None

    def add_game_to_stats(self, match: MatchShim):
        if not match.winner_id:
            return
        if match.get_winner() == self:
            self.wins += 1
            loser = match.get_loser()
            if loser:
                self.defeated.append(loser)
        else:
            self.losses += 1
        self.gd += match.get_score_from_player(self.id)[0] - match.get_score_from_player(self.id)[1]
        self.goals_scored += match.get_score_from_player(self.id)[0]


class GroupPlayerShim(PlayerShim):
    def __init__(self, id: int, name: str, country: str):
        super().__init__(id, name, country)
        self.wins = 0
        self.losses = 0
        self.gd = 0
        self.matches = {}

    def add_game_to_stats(self, match: MatchShim):
        self.matches[match.get_opponent(self.id)] = match
        if not match.winner_id:
            return
        if match.get_winner() == self:
            self.wins += 1
        else:
            self.losses += 1
        self.gd += match.get_score_from_player(self.id)[0] - match.get_score_from_player(self.id)[1]
