import trol

class Db(trol.Database):
    challenges = trol.Set(typ=trol.Model)
    ready = trol.Property(typ=bool)

    class Challenge(trol.Model):
        def __init__(self, name):
            self.id = name

        certificates = trol.Set(typ=trol.Model)
        ready = trol.Property(typ=bool)

    class Certificate(trol.Model):
        def __init__(self, cn, text=None):
            self.id = cn
            if text:
                self.text = text
        
        text = trol.Property()
