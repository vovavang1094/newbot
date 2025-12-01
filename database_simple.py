import json
import os

class Database:
    def __init__(self):
        self.blacklist = set()
        self.paused_alerts = set()
        self.alert_history = []
        
    async def connect(self):
        """Заглушка для совместимости"""
        pass
    
    async def get_blacklist(self):
        return self.blacklist
    
    async def add_to_blacklist(self, symbol: str):
        self.blacklist.add(symbol)
    
    async def remove_from_blacklist(self, symbol: str):
        self.blacklist.discard(symbol)
    
    async def get_paused_alerts(self):
        return self.paused_alerts
    
    async def add_paused_alert(self, symbol: str):
        self.paused_alerts.add(symbol)
    
    async def remove_paused_alert(self, symbol: str):
        self.paused_alerts.discard(symbol)
    
    async def save_alert(self, symbol: str, prev_volume: int, curr_volume: int, 
                         prev_price: float, curr_price: float, 
                         volume_change_pct: float, price_change_pct: float):
        self.alert_history.append({
            'symbol': symbol,
            'prev_volume': prev_volume,
            'curr_volume': curr_volume,
            'volume_change_pct': volume_change_pct,
            'price_change_pct': price_change_pct
        })
        # Храним только последние 1000 алертов
        if len(self.alert_history) > 1000:
            self.alert_history = self.alert_history[-1000:]
    
    async def get_recent_alerts(self, hours: int = 24):
        # В упрощенной версии возвращаем все алерты
        return self.alert_history
    
    async def close(self):
        pass

db = Database()
